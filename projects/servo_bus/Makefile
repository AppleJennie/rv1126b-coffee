# Makefile - STS3215 总线舵机工具集
# 目标平台: aarch64 Linux (ATK-DLRV1126B, Buildroot), 静态链接可直接上板运行

CC      ?= gcc
CFLAGS  ?= -Wall -O2 -std=c99
LDFLAGS  = -static

OBJS = sts_servo.o
BINS = servo_tool teach_record teach_play

all: $(BINS)

sts_servo.o: sts_servo.c sts_servo.h
	$(CC) $(CFLAGS) -c -o $@ $<

servo_tool: servo_tool.c $(OBJS) sts_servo.h
	$(CC) $(CFLAGS) -o $@ servo_tool.c $(OBJS) $(LDFLAGS)

teach_record: teach_record.c $(OBJS) sts_servo.h
	$(CC) $(CFLAGS) -o $@ teach_record.c $(OBJS) $(LDFLAGS)

teach_play: teach_play.c $(OBJS) sts_servo.h
	$(CC) $(CFLAGS) -o $@ teach_play.c $(OBJS) $(LDFLAGS)

clean:
	rm -f $(BINS) $(OBJS)

.PHONY: all clean
