/*
 * teach_play.c - 示教回放
 *
 * 读取 teach_record 生成的 CSV, 先把各舵机缓慢移到第一帧位置,
 * 再按原始时间戳节奏(-s 可调倍率)逐帧下发目标位置。
 *
 * 用法: teach_play -d /dev/ttyUSB0 -i out.csv [-s 1.0]
 */
#define _DEFAULT_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>

#include "sts_servo.h"

#define MAX_IDS    32
#define LINE_BUF   512
#define PLAY_SPEED 800   /* 回放时每帧携带的运行速度默认值 */

typedef struct {
    long long time_ms;       /* 相对时间戳 */
    int pos[MAX_IDS];        /* 各舵机位置 */
} frame_t;

static void usage(const char *prog)
{
    printf("usage: %s [-d device] [-b baud] -i in.csv [-s speed_scale]\n", prog);
    printf("  -d device       serial device (default /dev/ttyUSB0)\n");
    printf("  -b baud         baud rate (default 115200)\n");
    printf("  -i file         input CSV recorded by teach_record (required)\n");
    printf("  -s speed_scale  playback speed multiplier, >1 faster (default 1.0)\n");
}

static long long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;
}

/* 毫秒级睡眠 */
static void sleep_ms(long ms)
{
    if (ms <= 0)
        return;
    struct timespec ts;
    ts.tv_sec  = ms / 1000;
    ts.tv_nsec = (ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
}

/* 解析表头 "time_ms,id1,id2,...", 填 ids, 返回舵机数, 失败返回 -1 */
static int parse_header(char *line, int ids[])
{
    char *tok = strtok(line, ",\r\n");
    if (!tok || strcmp(tok, "time_ms") != 0)
        return -1;
    int n = 0;
    while ((tok = strtok(NULL, ",\r\n")) != NULL && n < MAX_IDS)
        ids[n++] = atoi(tok);
    return n > 0 ? n : -1;
}

/* 解析一帧数据行 "t,p1,p2,...", 成功返回 0 */
static int parse_frame(char *line, frame_t *f, int nids)
{
    char *tok = strtok(line, ",\r\n");
    if (!tok)
        return -1;
    f->time_ms = atoll(tok);
    for (int i = 0; i < nids; i++) {
        tok = strtok(NULL, ",\r\n");
        if (!tok)
            return -1;
        f->pos[i] = atoi(tok);
    }
    return 0;
}

int main(int argc, char *argv[])
{
    const char *device = "/dev/ttyUSB0";
    const char *infile = NULL;
    int baud = 115200;
    double scale = 1.0;
    int opt;

    while ((opt = getopt(argc, argv, "d:b:i:s:h")) != -1) {
        switch (opt) {
        case 'd': device = optarg; break;
        case 'b': baud   = atoi(optarg); break;
        case 'i': infile = optarg; break;
        case 's': scale  = atof(optarg); break;
        case 'h':
        default:
            usage(argv[0]);
            return (opt == 'h') ? 0 : 2;
        }
    }

    if (!infile || scale <= 0.0) {
        usage(argv[0]);
        return 2;
    }

    FILE *fp = fopen(infile, "r");
    if (!fp) {
        perror("fopen");
        return 1;
    }

    /* 读表头 */
    char line[LINE_BUF];
    if (!fgets(line, sizeof(line), fp)) {
        fprintf(stderr, "empty csv\n");
        fclose(fp);
        return 1;
    }
    int ids[MAX_IDS];
    int nids = parse_header(line, ids);
    if (nids < 1) {
        fprintf(stderr, "bad csv header, expect: time_ms,id1,id2,...\n");
        fclose(fp);
        return 1;
    }

    /* 读全部帧, 动态扩容 */
    int cap = 1024, nframes = 0;
    frame_t *frames = malloc((size_t)cap * sizeof(frame_t));
    if (!frames) {
        fprintf(stderr, "out of memory\n");
        fclose(fp);
        return 1;
    }
    while (fgets(line, sizeof(line), fp)) {
        if (nframes == cap) {
            cap *= 2;
            frame_t *nf = realloc(frames, (size_t)cap * sizeof(frame_t));
            if (!nf) {
                fprintf(stderr, "out of memory\n");
                free(frames);
                fclose(fp);
                return 1;
            }
            frames = nf;
        }
        if (parse_frame(line, &frames[nframes], nids) == 0)
            nframes++;
    }
    fclose(fp);

    if (nframes < 1) {
        fprintf(stderr, "no frames in %s\n", infile);
        free(frames);
        return 1;
    }
    printf("loaded %d frame(s), %d servo(s):", nframes, nids);
    for (int i = 0; i < nids; i++)
        printf(" id=%d", ids[i]);
    printf("\n");

    if (sts_open(device, baud) < 0) {
        fprintf(stderr, "failed to open %s @ %d\n", device, baud);
        free(frames);
        return 1;
    }

    int ret = 0;

    /* 缓慢移到第一帧位置 (速度 300, 给 2s 时间), 避免猛跳 */
    printf("moving to first frame...\n");
    for (int i = 0; i < nids; i++) {
        if (sts_write_position(ids[i], frames[0].pos[i], 300, 2000) < 0) {
            fprintf(stderr, "warn: goto start id=%d failed\n", ids[i]);
        }
    }
    sleep_ms(2500);

    /* 按原始节奏回放, 时间戳除以倍率 */
    printf("playing (x%.2f)...\n", scale);
    long long t0 = now_ms();
    for (int fi = 1; fi < nframes; fi++) {
        /* 等到该帧对应时刻 */
        long long target = t0 + (long long)(frames[fi].time_ms / scale);
        long long wait = target - now_ms();
        if (wait > 0)
            sleep_ms((long)wait);

        for (int i = 0; i < nids; i++) {
            int pos = frames[fi].pos[i];
            if (pos < STS_POS_MIN || pos > STS_POS_MAX)
                continue;   /* 录制时的无效读(-1)跳过 */
            if (sts_write_position(ids[i], pos, PLAY_SPEED, 0) < 0)
                fprintf(stderr, "warn: play id=%d frame=%d failed\n", ids[i], fi);
        }
    }

    printf("done, played %d frame(s)\n", nframes);
    sts_close();
    free(frames);
    return ret;
}
