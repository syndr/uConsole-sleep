VERSION ?= 1.6
DEB := uconsole-sleep.deb
APT_DEPS := python3-inotify python3-uinput python3-xlib swayidle sound-theme-freedesktop
SERVICES := sleep-power-control sleep-remap-powerkey sleep-cpu-governor
USER_SERVICES := sleep-idle-alert

SOURCES := $(wildcard src/*.py services/*.service services/user/*.service udev/*.rules) config.default make_uconsole-sleep_package.sh

.PHONY: all build deps install uninstall reinstall clean status logs user-status user-logs help

all: $(DEB)

build: $(DEB)

$(DEB): $(SOURCES)
	ENV_VERSION=$(VERSION) ./make_uconsole-sleep_package.sh

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
	systemctl --user status --no-pager $(USER_SERVICES)

user-logs:
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
