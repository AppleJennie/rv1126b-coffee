/******************************************************************
Copyright © Deng Zhimao Co., Ltd. 2021-2030. All rights reserved.
* @brief         widget.h
* @author        Deng Zhimao
* @email         dengzhimao@alientek.com/1252699831@qq.com
* @date          2025-04-10
* @link          http://www.alientek.com
* @LICENSE       GPLV3
*******************************************************************/
#ifndef WIDGET_H
#define WIDGET_H

#include <QWidget>
#include <QFile>

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

    QString state() const;
    void setState(const QString &newState);

    // 枚举
    enum State{
        Off = 0,   // 灭
        On         // 亮
    };

signals:
    void stateChanged();

private slots:
    void on_pushButton_clicked(bool checked);

private:
    Ui::Widget *ui;
    QString m_state; // LED状态
   mutable QFile m_ledFile; // LED控制文件
};
#endif // WIDGET_H
