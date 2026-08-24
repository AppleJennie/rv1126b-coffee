/*
 * Copyright (c) 2021 Rockchip, Inc. All Rights Reserved.
 *
 *  Licensed under the Apache License, Version 2.0 (the "License");
 *  you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *  See the License for the specific language governing permissions and
 *  limitations under the License.
 */
/*
 * Copyright (c) 2025 广州市星翼电子科技有限公司（正点原子）All Rights Reserved.
 *
 * @author: Deng Zhimao
 * @email: dengzhimao@alientek.com
 * B站作品: https://space.bilibili.com/474103963?spm_id_from=333.788.0.0

 * 开源电子网: http://www.openedv.com/forum.php
 * 正点原子官网: https://www.alientek.com
 * 店铺: https://zhengdianyuanzi.tmall.com
 *
 * LICENCE GPLV3
 */
#include "main.h"
#include <lvgl/lv_conf.h>
#include <lvgl/lvgl.h>

static int quit = 0;

static void sigterm_handler(int sig) {
    fprintf(stderr, "signal %d\n", sig);
    quit = 1;
}

int main(int argc, char **argv) {
    signal(SIGINT, sigterm_handler);

    // 一切LVGL应用的开始，必须加上这个初始化
    lv_port_init(0, 0, 0);

    /*****************************用户程序开始*************************************/
    // 创建一个屏幕对象
    lv_obj_t *scr = lv_scr_act();

    // 创建一个按钮
    lv_obj_t *btn = lv_btn_create(scr);
    lv_obj_set_pos(btn, 100, 100);
    lv_obj_set_size(btn, 120, 50);
    lv_obj_center(btn);
    /******************************结束******************************************/
    while (!quit) {
        /* 调用LVGL任务处理函数，LVGL所有的事件、绘制、送显等都在该接口内完成 */
        lv_task_handler();
        usleep(100);
    }

    return 0;
}
