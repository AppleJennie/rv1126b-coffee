/******************************************************************
Copyright © Deng Zhimao Co., Ltd. 2021-2030. All rights reserved.
* @brief         cameraframethread.cpp
* @author        Deng Zhimao
* @email         dengzhimao@alientek.com/1252699831@qq.com
* @date          2024-03-28
* @modifyDate    2025-12-10
* @modifyReason  适配RV1126B多缓冲区，同时修改为RGB565->RGB888
* @link          http://www.openedv.com/forum.php
*******************************************************************/
#include "cameraframethread.h"
#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>
#include <string.h>
#include <errno.h>
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <QDebug>
// 1. 使用selfpath取RGB流
// 2. grep '' /sys/class/video4linux/video*/name | grep selfpath
// 3. v4l2-ctl --list-formats-ext --device /dev/video24 | /dev/video32
// 4. v4l2-ctl -d /dev/video24 --get-fmt-video
#define VIDEO_DEV            "/dev/video24"
#define VIDEO_BUFFER_COUNT   3
#define VIDEO_MAX_PLANES     2

CameraFrameThread::CameraFrameThread(QObject *parent)
    : QThread{parent}
{}

void CameraFrameThread::run()
{
    // 定义所有变量
    int fd = -1;
    void *buffers[VIDEO_BUFFER_COUNT] = {nullptr};
    size_t buffer_sizes[VIDEO_BUFFER_COUNT] = {0};
    struct v4l2_format fmt = {0};
    struct v4l2_requestbuffers req = {0};
    struct v4l2_buffer buf = {0};
    struct v4l2_plane planes[VIDEO_MAX_PLANES];
    unsigned int mapped_buffers = 0;
    bool stream_started = false;

    // 1. 打开设备
    fd = open(VIDEO_DEV, O_RDWR | O_NONBLOCK);
    if (fd == -1) {
        qDebug("ERROR: failed to open video device %s: %s",
               VIDEO_DEV, strerror(errno));
        return;
    }

    // 2. 查询当前格式
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;

    if (ioctl(fd, VIDIOC_G_FMT, &fmt) == -1) {
        qDebug("ERROR: failed to VIDIOC_G_FMT: %s", strerror(errno));
        cleanup(fd, buffers, buffer_sizes, mapped_buffers, stream_started);
        return;
    }

    qDebug("Original format: %dx%d, pixelformat: 0x%08X, planes: %d",
           fmt.fmt.pix_mp.width, fmt.fmt.pix_mp.height,
           fmt.fmt.pix_mp.pixelformat, fmt.fmt.pix_mp.num_planes);

    // 3. 设置期望的格式
    fmt.fmt.pix_mp.width = 640;
    fmt.fmt.pix_mp.height = 480;
    fmt.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_RGB24;
    fmt.fmt.pix_mp.field = V4L2_FIELD_NONE;
    fmt.fmt.pix_mp.num_planes = 1;

    if (ioctl(fd, VIDIOC_S_FMT, &fmt) == -1) {
        qDebug("ERROR: failed to VIDIOC_S_FMT: %s", strerror(errno));
        cleanup(fd, buffers, buffer_sizes, mapped_buffers, stream_started);
        return;
    }

    // 4. 申请缓冲区
    req.count = VIDEO_BUFFER_COUNT;
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    req.memory = V4L2_MEMORY_MMAP;

    if (ioctl(fd, VIDIOC_REQBUFS, &req) == -1) {
        qDebug("ERROR: failed to VIDIOC_REQBUFS: %s", strerror(errno));
        cleanup(fd, buffers, buffer_sizes, mapped_buffers, stream_started);
        return;
    }

    if (req.count < VIDEO_BUFFER_COUNT) {
        qDebug("WARNING: Only %d buffers allocated (requested %d)",
               req.count, VIDEO_BUFFER_COUNT);
    }

    // 5. 映射所有缓冲区
    for (unsigned int i = 0; i < req.count; i++) {
        struct v4l2_buffer local_buf = {0};
        struct v4l2_plane local_planes[VIDEO_MAX_PLANES];

        local_buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        local_buf.memory = V4L2_MEMORY_MMAP;
        local_buf.index = i;
        local_buf.m.planes = local_planes;
        local_buf.length = VIDEO_MAX_PLANES;

        if (ioctl(fd, VIDIOC_QUERYBUF, &local_buf) == -1) {
            qDebug("ERROR: failed to VIDIOC_QUERYBUF for buffer %d: %s",
                   i, strerror(errno));
            cleanup(fd, buffers, buffer_sizes, mapped_buffers, stream_started);
            return;
        }

        qDebug("Buffer %d: %d planes, plane0 length: %u",
               i, local_buf.length, local_buf.m.planes[0].length);

        // 映射第一个plane（RGB只有1个plane）
        buffers[i] = mmap(NULL,
                          local_buf.m.planes[0].length,
                          PROT_READ | PROT_WRITE,
                          MAP_SHARED,
                          fd,
                          local_buf.m.planes[0].m.mem_offset);

        if (buffers[i] == MAP_FAILED) {
            qDebug("ERROR: failed to mmap buffer %d: %s",
                   i, strerror(errno));
            cleanup(fd, buffers, buffer_sizes, mapped_buffers, stream_started);
            return;
        }

        // 记录缓冲区大小
        buffer_sizes[i] = local_buf.m.planes[0].length;
        mapped_buffers++;

        qDebug("Mapped buffer %d at %p, size: %zu bytes",
               i, buffers[i], buffer_sizes[i]);

        // 立即放入队列
        if (ioctl(fd, VIDIOC_QBUF, &local_buf) == -1) {
            qDebug("ERROR: VIDIOC_QBUF failed for buffer %d: %s",
                   i, strerror(errno));
            cleanup(fd, buffers, buffer_sizes, mapped_buffers, stream_started);
            return;
        }
    }

    // 6. 启动流
    enum v4l2_buf_type stream_type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    if (ioctl(fd, VIDIOC_STREAMON, &stream_type) == -1) {
        qDebug("ERROR: VIDIOC_STREAMON failed: %s", strerror(errno));
        cleanup(fd, buffers, buffer_sizes, mapped_buffers, stream_started);
        return;
    }

    stream_started = true;
    qDebug("Stream started with %d buffers", req.count);

    // 7. 主捕获循环
    while (!isInterruptionRequested()) {
        // 设置查询参数
        memset(&buf, 0, sizeof(buf));
        memset(planes, 0, sizeof(planes));
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.m.planes = planes;
        buf.length = VIDEO_MAX_PLANES;

        // 等待数据（非阻塞模式）
        fd_set fds;
        struct timeval tv = {1, 0};  // 1秒超时

        FD_ZERO(&fds);
        FD_SET(fd, &fds);

        int ret = select(fd + 1, &fds, NULL, NULL, &tv);
        if (ret == -1) {
            qDebug("ERROR: select failed: %s", strerror(errno));
            break;
        }
        if (ret == 0) {
            // 超时，检查是否被中断
            if (isInterruptionRequested()) {
                break;
            }
            continue;
        }

        // 取出已填充的缓冲区
        if (ioctl(fd, VIDIOC_DQBUF, &buf) == -1) {
            if (errno == EAGAIN) {
                continue;  // 非阻塞，没有数据
            }
            qDebug("ERROR: VIDIOC_DQBUF failed: %s", strerror(errno));
            break;
        }

        // 处理图像
        if (buf.index < VIDEO_BUFFER_COUNT) {
            QImage qImage((unsigned char*)buffers[buf.index],
                          fmt.fmt.pix_mp.width,    // 使用 pix_mp
                          fmt.fmt.pix_mp.height,   // 使用 pix_mp
                          fmt.fmt.pix_mp.plane_fmt[0].bytesperline,
                          QImage::Format_BGR888);

            if (!qImage.isNull()) {
                emit cameraFrameIsReady(qImage);
            } else {
                qDebug("WARNING: Failed to create QImage from buffer %d", buf.index);
            }
        }

        // 重新放入队列
        if (ioctl(fd, VIDIOC_QBUF, &buf) == -1) {
            qDebug("ERROR: VIDIOC_QBUF failed: %s", strerror(errno));
            break;
        }
    }

    // 8. 清理资源
    cleanup(fd, buffers, buffer_sizes, mapped_buffers, stream_started);
    qDebug("Camera thread finished");
}

void CameraFrameThread::cleanup(int fd, void *buffers[], size_t buffer_sizes[],
                                unsigned int buffer_count, bool stream_started)
{
    // 1. 停止流
    if (stream_started && fd != -1) {
        enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        if (ioctl(fd, VIDIOC_STREAMOFF, &type) == -1) {
            qDebug("WARNING: VIDIOC_STREAMOFF failed: %s", strerror(errno));
        }
    }

    // 2. 解除映射
    for (unsigned int i = 0; i < buffer_count; i++) {
        if (buffers[i] != nullptr && buffers[i] != MAP_FAILED) {
            munmap(buffers[i], buffer_sizes[i]);
            buffers[i] = nullptr;
        }
    }

    // 3. 关闭设备
    if (fd != -1) {
        close(fd);
    }
}
