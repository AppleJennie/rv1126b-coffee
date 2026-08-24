/******************************************************************
Copyright © Deng Zhimao Co., Ltd. 2021-2030. All rights reserved.
* @projectName   qcamera
* @brief         widget.cpp
* @author        Deng Zhimao
* @email         dengzhimao@alientek.com/1252699831@qq.com
* @date          2024-03-26
* @modifyDate    2025-12-09
* @modifyReason  适配RV1126B
* @link          http://www.openedv.com/forum.php
*******************************************************************/
#include "widget.h"
#include <QDebug>
Widget::Widget(QWidget *parent)
    : QWidget(parent)
{
    this->resize(640, 480);
    // 请根据各自的摄像头节点填写
    // grep '' /sys/class/video4linux/video*/name | grep mainpath
	// grep '' /sys/class/video4linux/video*/name | grep selfpath
	// 使用mainpath或者selfpath
    m_qcamera = new QCamera("/dev/video23", this);

    if (!m_qcamera) {
        qDebug() << "摄像头初始化失败！";
    }

    QCameraViewfinderSettings settings;
    // 设置分辨率
    settings.setResolution(640, 480);
	// 注意：USB摄像头无需设置格式，默认为YUYV
    settings.setPixelFormat(QVideoFrame::Format_NV12);// USB摄像头不需要这行！同时你的USB摄像头需要支持YUYV！
    m_qcamera->setViewfinderSettings(settings);

    m_videoWidget = new QVideoWidget(this);
    m_videoWidget->resize(this->size());

    // 设置视频输出
    m_qcamera->setViewfinder(m_videoWidget);
    m_qcamera->start();
    m_videoWidget->show();
}

Widget::~Widget()
{

}

