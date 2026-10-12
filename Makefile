VERSION ?= 1.7
WITH_IDLE_ALERTER ?= 1
DEB := uconsole-sleep.deb
APT_DEPS := python3-inotify python3-uinput iw
SERVICES := sleep-power-control sleep-remap-powerkey sleep-cpu-governor
USER_SERVICES :=

ifeq ($(WITH_IDLE_ALERTER),1)
APT_DEPS += python3-xlib swayidle sound-theme-freedesktop
USER_SERVICES += sleep-idle-alert
endif

SOURCES := $(wildcard src/*.py services/*.service services/user/*.service udev/*.rules) config.default make_uconsole-sleep_package.sh

.PHONY: all build deps install uninstall reinstall clean status logs user-status user-logs help

all: $(DEB)

build: $(DEB)

$(DEB): $(SOURCES)
	ENV_VERSION=$(VERSION) WITH_IDLE_ALERTER=$(WITH_IDLE_ALERTER) ./make_uconsole-sleep_package.sh

deps:
	sudo apt install -y $(APT_DEPS)

install: $(DEB) deps
	sudo dpkg -i $(DEB)

reinstall: clean install

uninstall:
	sudo dpkg -r uconsole-sleep

clean:
	rm -f $(DEB)
	rm -rf uconsole-sleep

status:
	systemctl status --no-pager $(SERVICES)

logs:
	sudo journalctl -f $(addprefix -u ,$(SERVICES))

user-status:
	@if [ -z "$(USER_SERVICES)" ]; then echo "no user services in this build (WITH_IDLE_ALERTER=$(WITH_IDLE_ALERTER))"; exit 0; fi
	systemctl --user status --no-pager $(USER_SERVICES)

user-logs:
	@if [ -z "$(USER_SERVICES)" ]; then echo "no user services in this build (WITH_IDLE_ALERTER=$(WITH_IDLE_ALERTER))"; exit 0; fi
	journalctl --user -f $(addprefix -u ,$(USER_SERVICES))

help:
	@echo "Targets:"
	@echo "  build        build $(DEB) (default)"
	@echo "  deps         apt-install runtime dependencies"
	@echo "  install      build + install (depends on deps)"
	@echo "  reinstall    clean + install"
	@echo "  uninstall    remove the installed package"
	@echo "  clean        remove build artifacts"
	@echo "  status       systemctl status for all system services"
	@echo "  logs         follow journalctl for all system services"
	@echo "  user-status  systemctl --user status for user services"
	@echo "  user-logs    journalctl --user -f for user services"
	@echo ""
	@echo "Override version: make VERSION=1.7 build"
	@echo "Skip the idle alerter: make WITH_IDLE_ALERTER=0 build"
