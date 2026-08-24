/******************************************************************
Copyright © Deng Zhimao Co., Ltd. 2021-2030. All rights reserved.
* @brief         cameraframethread.h
* @author        Deng Zhimao
* @email         dengzhimao@alientek.com/1252699831@qq.com
* @date          2024-03-28
* @modifyDate    2025-12-10
* @modifyReason  适配RV1126B多缓冲区，同时修改为RGB565->RGB888
* @link          http://www.openedv.com/forum.php
*******************************************************************/
#ifndef CAMERAFRAMETHREAD_H
#define CAMERAFRAMETHREAD_H

#include <QObject>
#include <QThread>
#include <QImage>

class CameraFrameThread : public QThread
{
    Q_OBJECT
public:
    explicit CameraFrameThread(QObject *parent = nullptr);

protected:
    void run() override;

signals:
    void cameraFrameIsReady(QImage);
private:
    void cleanup(int fd, void *buffers[], size_t buffer_sizes[],
                 unsigned int buffer_count, bool stream_started);
};

#endif // CAMERAFRAMETHREAD_H
