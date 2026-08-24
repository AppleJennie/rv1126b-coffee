/******************************************************************
Copyright © Deng Zhimao Co., Ltd. 2021-2030. All rights reserved.
* @projectName   camera_opencv_test
* @brief         cameraframethread.cpp
* @author        Deng Zhimao
* @email         dengzhimao@alientek.com/1252699831@qq.com
* @date          2024-03-25
* @modifyDate    2025-12-09
* @modifyReason  适配RV1126B
* @link          http://www.openedv.com/forum.php
*******************************************************************/
#include "cameraframethread.h"
#include "opencv2/opencv.hpp"
#include "opencv2/imgproc.hpp"
#include "opencv2/imgcodecs.hpp"
#include "opencv2/core/core.hpp"
#include <QDebug>

CameraFrameThread::CameraFrameThread(QObject *parent): QThread(parent)
{

}

void CameraFrameThread::run()
{
    // 请根据各自的摄像头节点填写
    // grep '' /sys/class/video4linux/video*/name | grep mainpath
    // grep '' /sys/class/video4linux/video*/name | grep selfpath
    // 请使用selfpath节点，或mainpath节点都行
    // 避免转换，请使用selfpath节点，默认selfpath支持RGB
    cv::VideoCapture cap(24);
    cap.set(cv::CAP_PROP_FRAME_WIDTH, 640);
    cap.set(cv::CAP_PROP_FRAME_HEIGHT, 480);

    if (!cap.isOpened()) {
        return;
    }

    while (true) {
        cv::Mat frame;
        cap.read(frame);
        QImage tmpImage(frame.data, frame.cols, frame.rows, QImage::Format_BGR888);
        if(!tmpImage.isNull())
            emit imageIsReady(tmpImage);
    }
    cap.release();
}
