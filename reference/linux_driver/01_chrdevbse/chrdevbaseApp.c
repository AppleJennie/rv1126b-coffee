#include <stdio.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>

/***************************************************************
Copyright © ALIENTEK Co., Ltd. 1998-2029. All rights reserved.
文件名     : chrdevbaseApp.c
作者       : Alientek
版本       : V1.1
描述       : chrdevbase 测试 APP。
其他       : 使用方法：./chrdevbaseApp /dev/chrdevbase <1>|<2>
             argv[2] 1: 读驱动
             argv[2] 2: 写驱动
论坛       : www.openedv.com
日志       : V1.0 2025/4/21 初版 by Alientek
***************************************************************/

static char usrdata[] = "usr data from user space.";

int main(int argc, char *argv[])
{
    int fd, ret;
    char *filename;
    char readbuf[100] = {0};
    char writebuf[100] = {0};

    if (argc != 3) {
        printf("Usage: %s <device> <1:read | 2:write>\r\n", argv[0]);
        return -1;
    }

    filename = argv[1];

    fd = open(filename, O_RDWR);
    if (fd < 0) {
        perror("open");
        printf("Failed to open device file: %s\r\n", filename);
        return -1;
    }

    if (atoi(argv[2]) == 1) {
        /* 读驱动 */
        ret = read(fd, readbuf, sizeof(readbuf) - 1);
        if (ret < 0) {
            perror("read");
            printf("Read from %s failed!\r\n", filename);
        } else {
            readbuf[ret] = '\0';  // 确保字符串结尾
            printf("Read %d bytes: \"%s\"\r\n", ret, readbuf);
        }
    } else if (atoi(argv[2]) == 2) {
        /* 写驱动 */
        strncpy(writebuf, usrdata, sizeof(writebuf) - 1);
        ret = write(fd, writebuf, strlen(writebuf));
        if (ret < 0) {
            perror("write");
            printf("Write to %s failed!\r\n", filename);
        } else {
            printf("Wrote %d bytes: \"%s\"\r\n", ret, writebuf);
        }
    } else {
        printf("Invalid option: %s (1=read, 2=write)\r\n", argv[2]);
    }

    close(fd);
    return 0;
}
