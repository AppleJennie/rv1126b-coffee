/*
 * servo_tool.c - STS3215 总线舵机命令行工具
 *
 * 用法:
 *   servo_tool [-d device] [-b baud] <command> [args...]
 * 子命令:
 *   scan                          扫描总线上所有舵机
 *   read <id>                     打印位置/速度/负载/电压/温度
 *   move <id> <pos> [speed] [time_ms]   移动到目标位置
 *   torque <id> on|off            上力/卸力
 *   center <id...>                一批舵机回中位 2048
 */
#define _DEFAULT_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "sts_servo.h"

/* 打印用法帮助 */
static void usage(const char *prog)
{
    printf("STS3215 servo bus tool\n");
    printf("usage: %s [-d device] [-b baud] <command> [args...]\n", prog);
    printf("options:\n");
    printf("  -d device   serial device (default /dev/ttyUSB0)\n");
    printf("  -b baud     baud rate (default 115200)\n");
    printf("commands:\n");
    printf("  scan                              scan bus, print online ids\n");
    printf("  read <id>                         print position/speed/load/voltage/temperature\n");
    printf("  move <id> <pos> [speed] [time]    move to pos (0~4095, center 2048)\n");
    printf("  torque <id> on|off                enable/disable torque (off = free to move by hand)\n");
    printf("  center <id...>                    move servos to center position 2048\n");
    printf("examples:\n");
    printf("  %s scan\n", prog);
    printf("  %s -d /dev/ttyUSB1 read 1\n", prog);
    printf("  %s move 1 2048 800 500\n", prog);
    printf("  %s center 1 2 3\n", prog);
}

/* 解析整数参数, 非法时退出 */
static int parse_int(const char *s, const char *what)
{
    char *end = NULL;
    long v = strtol(s, &end, 10);
    if (!end || *end != '\0') {
        fprintf(stderr, "invalid %s: %s\n", what, s);
        exit(2);
    }
    return (int)v;
}

int main(int argc, char *argv[])
{
    const char *device = "/dev/ttyUSB0";
    int baud = 115200;
    int opt;

    while ((opt = getopt(argc, argv, "d:b:h")) != -1) {
        switch (opt) {
        case 'd': device = optarg; break;
        case 'b': baud = parse_int(optarg, "baud"); break;
        case 'h':
        default:
            usage(argv[0]);
            return (opt == 'h') ? 0 : 2;
        }
    }

    if (optind >= argc) {
        usage(argv[0]);
        return 0;
    }

    const char *cmd = argv[optind++];
    int narg = argc - optind;
    char **args = &argv[optind];
    int ret = 0;

    if (sts_open(device, baud) < 0) {
        fprintf(stderr, "failed to open %s @ %d\n", device, baud);
        return 1;
    }

    if (strcmp(cmd, "scan") == 0) {
        int ids[254];
        int n = sts_scan(ids, 254);
        printf("total: %d servo(s) online\n", n);
        if (n == 0)
            ret = 1;

    } else if (strcmp(cmd, "read") == 0) {
        if (narg < 1) { usage(argv[0]); ret = 2; goto out; }
        int id = parse_int(args[0], "id");
        sts_feedback_t fb;
        if (sts_read_feedback(id, &fb) < 0) {
            fprintf(stderr, "read id=%d failed (no response or bad checksum)\n", id);
            ret = 1;
        } else {
            printf("id=%d pos=%d speed=%d load=%d volt=%.1fV temp=%dC moving=%d\n",
                   id, fb.position, fb.speed, fb.load, fb.voltage,
                   fb.temperature, fb.moving);
        }

    } else if (strcmp(cmd, "move") == 0) {
        if (narg < 2) { usage(argv[0]); ret = 2; goto out; }
        int id    = parse_int(args[0], "id");
        int pos   = parse_int(args[1], "pos");
        int speed = (narg > 2) ? parse_int(args[2], "speed") : 0;
        int tms   = (narg > 3) ? parse_int(args[3], "time_ms") : 0;
        if (sts_write_position(id, pos, speed, tms) < 0) {
            fprintf(stderr, "move id=%d pos=%d failed\n", id, pos);
            ret = 1;
        } else {
            printf("id=%d -> pos=%d speed=%d time=%dms\n", id, pos, speed, tms);
        }

    } else if (strcmp(cmd, "torque") == 0) {
        if (narg < 2) { usage(argv[0]); ret = 2; goto out; }
        int id = parse_int(args[0], "id");
        int on;
        if (strcmp(args[1], "on") == 0)       on = 1;
        else if (strcmp(args[1], "off") == 0) on = 0;
        else { usage(argv[0]); ret = 2; goto out; }
        if (sts_torque(id, on) < 0) {
            fprintf(stderr, "torque id=%d %s failed\n", id, args[1]);
            ret = 1;
        } else {
            printf("id=%d torque %s\n", id, on ? "on" : "off");
        }

    } else if (strcmp(cmd, "center") == 0) {
        if (narg < 1) { usage(argv[0]); ret = 2; goto out; }
        for (int i = 0; i < narg; i++) {
            int id = parse_int(args[i], "id");
            /* 回中位: 速度 800, 时间 500ms, 平缓 */
            if (sts_write_position(id, STS_POS_CENTER, 800, 500) < 0) {
                fprintf(stderr, "center id=%d failed\n", id);
                ret = 1;
            } else {
                printf("id=%d -> center (%d)\n", id, STS_POS_CENTER);
            }
        }

    } else {
        fprintf(stderr, "unknown command: %s\n", cmd);
        usage(argv[0]);
        ret = 2;
    }

out:
    sts_close();
    return ret;
}
