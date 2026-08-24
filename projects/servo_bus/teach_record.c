/*
 * teach_record.c - 手掰示教录制
 *
 * 流程: 对给定 ID 全部卸力 -> 按指定频率采样各舵机当前位置,
 *       连同时间戳写入 CSV -> 到时(-t 秒)或 Ctrl-C 后恢复上力。
 *
 * 用法: teach_record -d /dev/ttyUSB0 -o out.csv -r 20 -t 60 id1 id2 ...
 * CSV 格式: 第一行表头 time_ms,id1,id2,..., 之后每行一帧。
 */
#define _DEFAULT_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <signal.h>
#include <time.h>

#include "sts_servo.h"

#define MAX_IDS 32

static volatile sig_atomic_t g_stop = 0;

/* Ctrl-C 置停止标志, 主循环负责恢复上力 */
static void on_sigint(int sig)
{
    (void)sig;
    g_stop = 1;
}

/* 单调时钟, 毫秒 */
static long long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;
}

static void usage(const char *prog)
{
    printf("usage: %s [-d device] [-b baud] -o out.csv [-r rate_hz] [-t seconds] id1 id2 ...\n", prog);
    printf("  -d device    serial device (default /dev/ttyUSB0)\n");
    printf("  -b baud      baud rate (default 115200)\n");
    printf("  -o file      output CSV file (required)\n");
    printf("  -r rate_hz   sample rate (default 20)\n");
    printf("  -t seconds   record duration (default 60)\n");
}

int main(int argc, char *argv[])
{
    const char *device = "/dev/ttyUSB0";
    const char *outfile = NULL;
    int baud = 115200;
    int rate = 20;
    int duration = 60;
    int opt;

    while ((opt = getopt(argc, argv, "d:b:o:r:t:h")) != -1) {
        switch (opt) {
        case 'd': device   = optarg; break;
        case 'b': baud     = atoi(optarg); break;
        case 'o': outfile  = optarg; break;
        case 'r': rate     = atoi(optarg); break;
        case 't': duration = atoi(optarg); break;
        case 'h':
        default:
            usage(argv[0]);
            return (opt == 'h') ? 0 : 2;
        }
    }

    int nids = argc - optind;
    if (!outfile || nids < 1 || nids > MAX_IDS || rate < 1 || duration < 1) {
        usage(argv[0]);
        return 2;
    }

    int ids[MAX_IDS];
    for (int i = 0; i < nids; i++)
        ids[i] = atoi(argv[optind + i]);

    FILE *fp = fopen(outfile, "w");
    if (!fp) {
        perror("fopen");
        return 1;
    }

    if (sts_open(device, baud) < 0) {
        fprintf(stderr, "failed to open %s @ %d\n", device, baud);
        fclose(fp);
        return 1;
    }

    /* 表头: time_ms,id1,id2,... */
    fprintf(fp, "time_ms");
    for (int i = 0; i < nids; i++)
        fprintf(fp, ",%d", ids[i]);
    fprintf(fp, "\n");

    /* 全部卸力, 允许手掰 */
    for (int i = 0; i < nids; i++) {
        if (sts_torque(ids[i], 0) < 0)
            fprintf(stderr, "warn: torque off id=%d failed\n", ids[i]);
    }
    printf("torque off, start recording %d servo(s) @ %dHz for %ds -> %s\n",
           nids, rate, duration, outfile);
    printf("press Ctrl-C to stop early\n");

    signal(SIGINT, on_sigint);

    long long t0 = now_ms();
    long long period_ms = 1000 / rate;
    long long frames = 0;
    int ret = 0;

    while (!g_stop && (now_ms() - t0) < (long long)duration * 1000LL) {
        long long frame_start = now_ms();
        long long rel = frame_start - t0;

        /* 采样一帧: 逐个读当前位置 */
        fprintf(fp, "%lld", rel);
        for (int i = 0; i < nids; i++) {
            int pos = -1;
            if (sts_read_position(ids[i], &pos) < 0)
                fprintf(stderr, "warn: read id=%d failed\n", ids[i]);
            fprintf(fp, ",%d", pos);
        }
        fprintf(fp, "\n");
        fflush(fp);
        frames++;

        /* 按周期补齐剩余时间 */
        long long elapsed = now_ms() - frame_start;
        if (elapsed < period_ms) {
            struct timespec ts;
            long left = (long)(period_ms - elapsed);
            ts.tv_sec  = left / 1000;
            ts.tv_nsec = (left % 1000) * 1000000L;
            nanosleep(&ts, NULL);
        }
    }

    /* 恢复上力 */
    for (int i = 0; i < nids; i++) {
        if (sts_torque(ids[i], 1) < 0) {
            fprintf(stderr, "warn: torque on id=%d failed\n", ids[i]);
            ret = 1;
        }
    }

    sts_close();
    fclose(fp);
    printf("done, %lld frames recorded to %s%s\n",
           frames, outfile, g_stop ? " (interrupted)" : "");
    return ret;
}
