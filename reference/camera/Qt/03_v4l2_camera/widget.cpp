/******************************************************************
Copyright © Deng Zhimao Co., Ltd. 2021-2030. All rights reserved.
* @brief         widget.h
* @author        Deng Zhimao
* @email         dengzhimao@alientek.com/1252699831@qq.com
* @date          2024-03-28
* @link          http://www.openedv.com/forum.php
*******************************************************************/
#include "widget.h"
#include "ui_widget.h"

Widget::Widget(QWidget *parent)
    : QWidget(parent)
    , ui(new Ui::Widget)
{
    ui->setupUi(this);
    m_cameraFrameThread = new CameraFrameThread(this);
    connect(m_cameraFrameThread, SIGNAL(cameraFrameIsReady(QImage)), this, SLOT(updateImage(QImage)));
    m_cameraFrameThread->start();
}

Widget::~Widget()
{
    delete ui;
}

void Widget::updateImage(QImage image)
{
    ui->label->setPixmap(QPixmap::fromImage(image));
}
