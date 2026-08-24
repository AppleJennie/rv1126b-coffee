#@author Deng Zhimao
#@email dengzhimao@alientek.com
#http://www.openedv.com/forum.php

cmake_minimum_required(VERSION 3.8)
message(STATUS "cmake version ${CMAKE_VERSION}")

#请根据各自己的开发板设置相应的路径
#SDK/buildroot/output/alientek_rv1126b/host路径就是工具链路径，或者设置为提供的工具链/opt/atk-dlrv1126b-toolchain
set(TOOLCHAIN_DIR /opt/atk-dlrv1126b-toolchain)
set(CMAKE_CXX_COMPILER ${TOOLCHAIN_DIR}/bin/aarch64-buildroot-linux-gnu-g++)
set(CMAKE_C_COMPILER ${TOOLCHAIN_DIR}/bin/aarch64-buildroot-linux-gnu-gcc)
set(SYSROOT ${TOOLCHAIN_DIR}/aarch64-buildroot-linux-gnu/sysroot/usr/include)
set(CMAKE_SYSROOT ${TOOLCHAIN_DIR}/aarch64-buildroot-linux-gnu/sysroot)
set(CMAKE_CXX_STANDARD 11)
add_definitions(-g -O0 -ggdb -gdwarf -funwind-tables -rdynamic)
add_definitions(-Wno-write-strings -Wno-return-type)

set(OPENCV_LIBS opencv_core opencv_flann opencv_videoio opencv_video
opencv_highgui opencv_imgcodecs opencv_imgproc)

include_directories(${SYSROOT})
include_directories(${SYSROOT}/opencv4)

project(opencv)
add_executable(opencv opencv.cpp)
target_link_libraries(opencv ${OPENCV_LIBS})
add_custom_target(distclean
    COMMAND ${CMAKE_COMMAND} -E remove CMakeCache.txt
    COMMAND ${CMAKE_COMMAND} -E remove Makefile
    COMMAND ${CMAKE_COMMAND} -E remove_directory CMakeFiles
    COMMAND ${CMAKE_COMMAND} -E remove cmake_install.cmake
    COMMAND ${CMAKE_COMMAND} -E remove ${PROJECT_NAME}
    COMMENT "Clean all CMake generated files and ${PROJECT_NAME}"
)
