#include <opencv2/opencv.hpp>
#include <iostream>

int main(int argc, char** argv)
{
    // 创建一个VideoCapture对象，video0,写0
	// RV1126B MIPI摄像头填23/24或者31/32 USB摄像头填52
    cv::VideoCapture cap(23);

    // 检查摄像头是否成功打开
    if (!cap.isOpened()) {
        std::cerr << "Error opening video capture" << std::endl;
        return -1;
    }

    // 设置分辨率为640x480
    cap.set(cv::CAP_PROP_FRAME_WIDTH, 640);
    cap.set(cv::CAP_PROP_FRAME_HEIGHT, 480);

    // 创建一个窗口来显示视频
    cv::namedWindow("Camera Feed", cv::WINDOW_AUTOSIZE);

    // 循环读取摄像头的帧
    while (true) {
        // 读取一帧
        cv::Mat frame;
        if (!cap.read(frame)) {
            std::cerr << "Failed to grab frame" << std::endl;
            break;
        }

        // 显示帧
        cv::imshow("Camera Feed", frame);

        // 等待30毫秒，如果用户在这段时间内按下了'q'键，则退出循环
        if (cv::waitKey(30) == 'q') {
            break;
        }
    }

    // 释放VideoCapture对象和销毁所有窗口
    cap.release();
    cv::destroyAllWindows();

    return 0;
}