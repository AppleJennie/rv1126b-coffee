/******************************************************************
Copyright © Deng Zhimao Co., Ltd. 2021-2030. All rights reserved.
* @brief         widget.h
* @author        Deng Zhimao
* @email         dengzhimao@alientek.com/1252699831@qq.com
* @date          2024-03-28
* @link          http://www.openedv.com/forum.php
*******************************************************************/
#ifndef WIDGET_H
#define WIDGET_H

#include <QWidget>
#include "cameraframethread.h"

QT_BEGIN_NAMESPACE
namespace Ui {
class Widget;
}
QT_END_NAMESPACE

class Widget : public QWidget
{
    Q_OBJECT

public:
    Widget(QWidget *parent = nullptr);
    ~Widget();

private:
    Ui::Widget *ui;
    CameraFrameThread *m_cameraFrameThread;

private slots:
    void updateImage(QImage);
};
#endif // WIDGET_H
