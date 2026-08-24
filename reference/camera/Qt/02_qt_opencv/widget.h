#ifndef WIDGET_H
#define WIDGET_H

#include <QWidget>
#include "cameraframethread.h"
QT_BEGIN_NAMESPACE
namespace Ui { class Widget; }
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
