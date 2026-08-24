#!/bin/bash
if [ -f /opt/atk-dlrv1126b-toolchain/environment-setup ]; then
    echo "作者：dengzhimao@alientek.com"
    echo "B站：dengzhimao"
    echo "license       LGPL-2.1-or-later"
else
    echo "警告：请按根据网盘资料下/10用户手册/辅助文档/基于Buildroot系统_交叉编译器安装与使用参考手册，安装交叉编译器再编译!"
    exit -1
fi
source /opt/atk-dlrv1126b-toolchain/environment-setup

cmake .
make -j 16
