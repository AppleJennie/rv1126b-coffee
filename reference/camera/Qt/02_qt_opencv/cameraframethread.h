/******************************************************************
Copyright © Deng Zhimao Co., Ltd. 2021-2030. All rights reserved.
* @projectName   camera_opencv_test
* @brief         cameraframethread.h
* @author        Deng Zhimao
* @email         dengzhimao@alientek.com/1252699831@qq.com
* @date          2024-03-25
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
    CameraFrameThread(QObject *parent = nullptr);
protected:
    virtual void run() override;
signals:
    void imageIsReady(QImage);
};

#endif // CAMERAFRAMETHREAD_H
