/******************************************************************
Copyright © Deng Zhimao Co., Ltd. 2021-2030. All rights reserved.
* @brief         widget.cpp
* @author        Deng Zhimao
* @email         dengzhimao@alientek.com/1252699831@qq.com
* @date          2025-04-10
* @link          http://www.alientek.com
* @LICENSE       GPLV3
*******************************************************************/
#include "widget.h"
#include "ui_widget.h"
#include <QDebug>

Widget::Widget(QWidget *parent)
    : QWidget(parent)
    , ui(new Ui::Widget)
{
    ui->setupUi(this);
    // 设置全屏
    this->showFullScreen();
    // 修改为手动控制，当然你可以用QFile写入“none”
    system("echo \"none\" > /sys/class/leds/work/trigger");
    // 设置led控制文件
    m_ledFile.setFileName("/sys/class/leds/work/brightness");
    // 程序初始化时，先读取LED状态
    if (m_ledFile.open(QIODevice::ReadOnly)) {
        setState(m_ledFile.readAll().simplified());
        m_ledFile.close(); // 打开需要关闭
    }
    ui->pushButton->setCheckable(true);// 设置可被选中
}

Widget::~Widget()
{
    delete ui;
}

QString Widget::state() const
{
    return m_state;
}

void Widget::setState(const QString &newState)
{
    if (m_state == newState)
        return;
    m_state = newState;
    if (bool(m_state == "1") == State::On) {
        ui->pushButton->setText("关灯");
        // 设置不同的颜色
        ui->pushButton->setStyleSheet("QPushButton{background-color: rgb(239, 41, 41)}");
    } else {
        ui->pushButton->setText("开灯");
        ui->pushButton->setStyleSheet("QPushButton{background-color: rgb(136, 138, 133)}");
    }
    emit stateChanged();
}

void Widget::on_pushButton_clicked(bool checked)
{
    // 被选中时，开灯
    if (checked) {
        if(m_ledFile.open(QIODevice::ReadWrite)) {
            m_ledFile.write("1");
            ui->pushButton->setText("关灯");
            ui->pushButton->setStyleSheet("QPushButton{background-color: rgb(239, 41, 41)}");
            m_ledFile.close();
        }
    } else {
        if(m_ledFile.open(QIODevice::ReadWrite)) {
            m_ledFile.write("0");
            ui->pushButton->setText("开灯");
            ui->pushButton->setStyleSheet("QPushButton{background-color: rgb(136, 138, 133)}");
            m_ledFile.close();
        }
    }
}

