#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <unistd.h>
#include <sys/epoll.h>
#include <sys/poll.h>
#include <linux/input.h>
#include <fcntl.h>
#include <dirent.h>

struct position {
    int x, y;
    int synced;
    struct input_absinfo xi, yi;
};

struct ev {
    struct pollfd *fd;

    struct virtualkey *vks;
    int vk_count;

    char deviceName[64];

    int ignored;

    struct position p, mt_p;
    int down;
};

#define MAX_DEVICES 16
static unsigned ev_count = 0;
static struct pollfd ev_fds[MAX_DEVICES];
static struct ev evs[MAX_DEVICES];

int ev_init(void)
{
    DIR *dir;
    struct dirent *de;
    int fd;
    char name[80];
    dir = opendir("/dev/input");
    if(dir != 0) {
        while((de = readdir(dir))) {
            if(strncmp(de->d_name, "event",5))
                continue;
            fd = openat(dirfd(dir), de->d_name, O_RDONLY);
            if(fd < 0) continue;
            else {
                if (ioctl(fd, EVIOCGNAME(sizeof(name) - 1), &name) < 1) {
                    name[0] = '\0';
                }
                if (strcmp(name, "adc-keys")) {
                    close(fd);
                    continue;
                }
            }
            ev_fds[ev_count].fd = fd;
            ev_fds[ev_count].events = POLLIN;
            evs[ev_count].fd = &ev_fds[ev_count];

            ev_count++;
            if(ev_count == MAX_DEVICES) break;
            break;
        }
    }

    return 0;
}

int ev_get(struct input_event *ev, unsigned dont_wait)
{
    int r;
    unsigned n;

    do {
        r = poll(ev_fds, ev_count, dont_wait ? 0 : -1);

        if(r > 0) {
            for(n = 0; n < ev_count; n++) {
                if(ev_fds[n].revents & POLLIN) {
                    r = read(ev_fds[n].fd, ev, sizeof(*ev));
                    if(r == sizeof(*ev)) {
                        //if (!vk_modify(&evs[n], ev))
                        return 0;
                    }
                }
            }
        }
    } while(dont_wait == 0);

    return -1;
}


int main(void)
{
    struct input_event ev;

    setbuf(stdout, NULL);
    printf("===  等待按键按下  ===\n");

    if (ev_init() != 0)
        goto err_out;

    for ( ; ; ) {
        if (ev_get(&ev, 0) == 0) {
            if (EV_KEY == ev.type) {
                if (ev.value == 0) {
                    switch(ev.code) {
                    case KEY_ESC:
                        printf("KEY_ESC: 松开\n");
                        break;
                    case KEY_VOLUMEUP:
                        printf("KEY_VOLUMEUP: 松开\n");
                        break;
                    case KEY_VOLUMEDOWN:
                        printf("KEY_VOLUMEDOWN: 松开\n");
                        break;
                    case KEY_MENU:
                        printf("KEY_MENU: 松开\n");
                        break;
                    }
                }
                else if (ev.value == 1) {
                    switch(ev.code) {
                    case KEY_ESC:
                        printf("KEY_ESC: 按下\n");
                        break;
                    case KEY_VOLUMEUP:
                        printf("KEY_VOLUMEUP: 按下\n");
                        break;
                    case KEY_VOLUMEDOWN:
                        printf("KEY_VOLUMEDOWN: 按下\n");
                        break;
                    case KEY_MENU:
                        printf("KEY_MENU: 按下\n");
                        break;
                    }
                }
            }
        }
    }

succ_out:
    printf("=== end of key test ===\n");
    return 0;

err_out:
    printf("=== end of key test ===\n");
    return -1;
}
