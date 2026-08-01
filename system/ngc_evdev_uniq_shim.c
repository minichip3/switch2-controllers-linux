/*
 * LD_PRELOAD shim: SDL3 linux evdev pairs gamepads with IMU nodes via EVIOCGUNIQ.
 * uinput devices expose phys (ngc/MAC) but uniq ioctl returns ENOENT, so
 * SDL_GamepadHasSensor stays false for ngc virtual pads.
 *
 * When uniq is missing, return phys for devices whose phys starts with "ngc/".
 *
 * Build: scripts/build-ngc-evdev-shim.sh
 * Use:   LD_PRELOAD=$HOME/.local/lib/libngc_evdev_uniq.so dusklight
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <linux/input.h>
#include <stdarg.h>
#include <string.h>
#include <sys/ioctl.h>

#ifndef EVIOCGUNIQ
#define EVIOCGUNIQ(size) _IOC(_IOC_READ, 'E', 0x01, size)
#endif
#ifndef EVIOCGPHYS
#define EVIOCGPHYS(size) _IOC(_IOC_READ, 'E', 0x07, size)
#endif

static int (*real_ioctl)(int, unsigned long, ...) = NULL;

static int is_evdev_uniq_request(unsigned long request)
{
	return _IOC_TYPE(request) == 'E' && _IOC_NR(request) == 0x01;
}

static int ngc_phys_uniq(int fd, void *buf, size_t len)
{
	char phys[256];

	if (!buf || len == 0) {
		errno = EFAULT;
		return -1;
	}

	memset(phys, 0, sizeof(phys));
	if (real_ioctl(fd, EVIOCGPHYS(sizeof(phys) - 1), phys) < 0) {
		return -1;
	}
	if (strncmp(phys, "ngc/", 4) != 0) {
		return -1;
	}

	strncpy((char *)buf, phys, len - 1);
	((char *)buf)[len - 1] = '\0';
	return 0;
}

int ioctl(int fd, unsigned long request, ...)
{
	va_list ap;
	void *arg;
	int ret;

	if (!real_ioctl) {
		real_ioctl = dlsym(RTLD_NEXT, "ioctl");
		if (!real_ioctl) {
			errno = ENOSYS;
			return -1;
		}
	}

	va_start(ap, request);
	arg = va_arg(ap, void *);
	va_end(ap);

	ret = real_ioctl(fd, request, arg);
	if (!is_evdev_uniq_request(request)) {
		return ret;
	}

	if (ret >= 0 && arg) {
		const char *uniq = (const char *)arg;
		if (uniq[0] != '\0') {
			return ret;
		}
	}

	return ngc_phys_uniq(fd, arg, _IOC_SIZE(request));
}
