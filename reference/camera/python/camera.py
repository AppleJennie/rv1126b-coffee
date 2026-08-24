import cv2

# 打开摄像头，例video9，写9
# RV1126B MIPI摄像头填23/24或者31/32 USB摄像头填52
cap = cv2.VideoCapture(23)

# 检查摄像头是否成功打开
if not cap.isOpened():
    print("无法打开摄像头")
    exit()

# 设置摄像头的分辨率
# 假设我们想要设置为 640x480
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

# 循环读取摄像头的帧
while True:
    # 读取一帧
    ret, frame = cap.read()

    # 检查是否成功读取帧
    if not ret:
        print("无法接收帧（流可能已结束？）")
        break

    # 显示帧
    cv2.imshow('frame', frame)

    # 如果按下'q'键，则退出循环
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

# 释放摄像头并关闭所有窗口
cap.release()
cv2.destroyAllWindows()