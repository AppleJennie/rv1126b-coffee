/******************************************************************
Copyright © Deng Zhimao Co., Ltd. 2021-2030. All rights reserved.
* @projectName   qcamera
* @brief         widget.cpp
* @author        Deng Zhimao
* @email         dengzhimao@alientek.com/1252699831@qq.com
* @date          2024-03-26
* @link          http://www.openedv.com/forum.php
* @LICENSE       GPLV3
*******************************************************************/
#include "widget.h"
#include <QDebug>
#include <QVBoxLayout>
#include <QPushButton>
#include <QLabel>
#include <QFont>
#include <QCamera>
#include <QVideoWidget>
#include <QCameraImageCapture>
Widget::Widget(QWidget *parent)
    : QWidget(parent)
{
    // 全屏显示
    this->showFullScreen();
    // 请根据各自的摄像头节点填写,RV1126B USB摄像头一般就是video52
   QCamera *m_qcamera = new QCamera("/dev/video52", this);

    if (!m_qcamera) {
        qDebug() << "摄像头初始化失败！";
    }

    // 字体大小
    QFont font;
    font.setPixelSize(30);

    //　布局
    QVBoxLayout *layout = new QVBoxLayout(this);
    layout->setAlignment(Qt::AlignCenter);

    QLabel *titleLabel = new QLabel();
    titleLabel->setText("正点原子Ｑt赋能USB摄像头的摄影功能开发实战");
    titleLabel->setFixedSize(this->width(), 100);
    titleLabel->setFont(font);
    titleLabel->setAlignment(Qt::AlignCenter);
    layout->addWidget(titleLabel);

    QCameraViewfinderSettings settings;
    // 设置分辨率
    settings.setResolution(640, 480);
    m_qcamera->setViewfinderSettings(settings);

    QVideoWidget *m_videoWidget = new QVideoWidget();
    // 设置大小
    m_videoWidget->setFixedSize(this->width(), 480);

    layout->addWidget(m_videoWidget);

    // 拍照按钮
    QPushButton *captureButton = new QPushButton();
    captureButton->setFixedSize(this->width(), 100);
    captureButton->setText("拍照");

    captureButton->setFont(font);

    layout->addWidget(captureButton);

    QLabel *captureLabel = new QLabel();
    captureLabel->setFixedSize(this->width(), 480);

    // 显示拍照
    layout->addWidget(captureLabel);

    // 设置视频输出
    m_qcamera->setViewfinder(m_videoWidget);
    m_qcamera->start();

    m_videoWidget->show();

    QCameraImageCapture *imageCapture = new QCameraImageCapture(m_qcamera, this);

    // 连接拍照按钮的点击信号到拍照槽函数
    QObject::connect(captureButton, &QPushButton::clicked, [imageCapture,captureLabel]() {
        // 拍照,拍照路径默认为/路径下
        imageCapture->capture("/imageCapture.jpg");
    });

    QObject::connect(imageCapture, &QCameraImageCapture::imageSaved, [captureLabel]() {
        // 拍照完成
        QImage image("/imageCapture.jpg");
        if (!image.isNull()) {
            captureLabel->setPixmap(QPixmap::fromImage(image));
        }
    });
}

Widget::~Widget()
{

}

