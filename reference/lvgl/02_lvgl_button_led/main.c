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

// 字库结构体实例
lv_ft_info_t ft_info;
// 按钮
static lv_obj_t *btn;
// 标签
static lv_obj_t *label;

// 开发板中字体库路径
#define FREETYPE_FONT_FILE ("/usr/share/fonts/source-han-sans-cn/SourceHanSansCN-Normal.otf")

#define LED_BRIGHTNESS_PATH "/sys/class/leds/work/brightness"

static void sigterm_handler(int sig) {
    fprintf(stderr, "signal %d\n", sig);
    quit = 1;
}

// 读取led状态
int read_brightness() {
    FILE *file = fopen(LED_BRIGHTNESS_PATH, "r");
    if (!file) {
        LV_LOG_USER("Failed to open brightness file for reading");
        return -1;
    }
    int brightness;
    if (fscanf(file, "%d", &brightness) != 1) {
        LV_LOG_USER("Failed to read brightness value");
        fclose(file);
        return -1;
    }
    fclose(file);
    return brightness;
}

// 控制led
int write_brightness(int brightness) {
    FILE *file = fopen(LED_BRIGHTNESS_PATH, "w");
    if (!file) {
        LV_LOG_USER("Failed to open brightness file for writing");
        return -1;
    }

    if (fprintf(file, "%d\n", brightness) < 0) {
        LV_LOG_USER("Failed to write brightness value");
        fclose(file);
        return -1;
    }

    fclose(file);
    return 0;
}

static void event_handler(lv_event_t *e) {
    lv_event_code_t code = lv_event_get_code(e);
    // 获取触发事件的对象
    lv_obj_t *target = lv_event_get_target(e);

    if (code == LV_EVENT_CLICKED) {
        LV_LOG_USER("Clicked");
        if (target == btn) {
            LV_LOG_USER("btn Clicked");
        }
    }

    if (code == LV_EVENT_VALUE_CHANGED) {
        // 根据按钮状态更新标签文本
        if (lv_obj_has_state(target, LV_STATE_CHECKED)) {
            lv_label_set_text(label, "关灯");
            write_brightness(1);
        } else {
            lv_label_set_text(label, "开灯");
            write_brightness(0);
        }
    }
}

int main(int argc, char **argv) {
    signal(SIGINT, sigterm_handler);

    // 一切LVGL应用的开始，必须加上这个初始化
    lv_port_init(0, 0, 0);

    /*****************************用户程序开始*************************************/
    ft_info.name = FREETYPE_FONT_FILE; // 使用绝对路径指定字体文件
    ft_info.weight = 50;               // 字体大小
    ft_info.style = FT_FONT_STYLE_NORMAL;
    ft_info.mem = NULL;

    // 初始化字体
    if (!lv_ft_font_init(&ft_info)) {
        printf("create failed.");
    }

    // 将心跳改为手动触发
    system("echo none > /sys/class/leds/work/trigger");

    int brightness = read_brightness();
    if (brightness != -1) {
        LV_LOG_USER("Current brightness: %d\n", brightness);
    }

    // 创建一个样式用于设置默认状态
    static lv_style_t style_default;
    lv_style_init(&style_default);
    lv_style_set_bg_opa(&style_default, LV_OPA_COVER);
    lv_style_set_bg_color(&style_default, lv_palette_main(LV_PALETTE_GREY)); // 默认状态颜色
    lv_style_set_radius(&style_default, 0);

    // 创建一个样式用于设置按下状态
    static lv_style_t style_pressed;
    lv_style_init(&style_pressed);
    lv_style_set_bg_opa(&style_pressed, LV_OPA_COVER);
    lv_style_set_bg_color(&style_pressed, lv_palette_main(LV_PALETTE_BLUE)); // 按下状态颜色
    lv_style_set_radius(&style_pressed, 0);

    // 创建一个按钮
    btn = lv_btn_create(lv_scr_act());
    lv_obj_set_size(btn, lv_obj_get_width(lv_scr_act()), lv_obj_get_height(lv_scr_act()));
    // 将样式应用到按钮
    lv_obj_add_style(btn, &style_default, LV_STATE_DEFAULT);
    lv_obj_add_style(btn, &style_pressed, LV_STATE_CHECKED);
    label = lv_label_create(btn);
    lv_obj_set_style_text_font(label, ft_info.font, LV_STATE_DEFAULT);
    lv_obj_add_flag(btn, LV_OBJ_FLAG_CHECKABLE);
    lv_obj_center(label);

    if (brightness > 0) {
        lv_label_set_text_fmt(label, "关灯");
    } else {
        lv_label_set_text_fmt(label, "开灯");
    }
    lv_obj_add_state(btn, brightness > 0 ? LV_STATE_CHECKED : LV_STATE_DEFAULT);

    lv_obj_add_event_cb(btn, event_handler, LV_EVENT_ALL, NULL);

    /******************************结束******************************************/
    while (!quit) {
        /* 调用LVGL任务处理函数，LVGL所有的事件、绘制、送显等都在该接口内完成 */
        lv_task_handler();
        usleep(100);
    }

    return 0;
}
