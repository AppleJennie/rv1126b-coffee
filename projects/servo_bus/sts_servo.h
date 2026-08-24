/*
 * sts_servo.h - Feetech STS 系列总线舵机 (STS3215) 协议层
 *
 * 半双工 TTL 总线，数据包格式:
 *   0xFF 0xFF, ID, Length, Instruction/Error, Param1..ParamN, Checksum
 *   Length   = 参数个数 + 2 (Instruction + Checksum)
 *   Checksum = ~(ID + Length + Instruction + 所有Param) & 0xFF
 */
#ifndef STS_SERVO_H
#define STS_SERVO_H

#include <stdint.h>

/* 指令码 */
#define STS_INST_PING        0x01
#define STS_INST_READ        0x02
#define STS_INST_WRITE       0x03
#define STS_INST_REG_WRITE   0x04
#define STS_INST_ACTION      0x05
#define STS_INST_SYNC_WRITE  0x83

/* 广播 ID */
#define STS_ID_BROADCAST     0xFE

/* STS3215 寄存器地址 */
#define STS_REG_TORQUE_SWITCH   40  /* 1 字节: 0=卸力, 1=上力 */
#define STS_REG_ACCELERATION    41  /* 1 字节 */
#define STS_REG_TARGET_POS      42  /* 2 字节小端, 0~4095, 中位 2048 */
#define STS_REG_RUNNING_TIME    44  /* 2 字节小端 */
#define STS_REG_RUNNING_SPEED   46  /* 2 字节小端 */
#define STS_REG_TORQUE_LIMIT    48  /* 1 字节 */
#define STS_REG_CURRENT_POS     56  /* 2 字节小端, 只读 */
#define STS_REG_CURRENT_SPEED   58  /* 2 字节, 只读 */
#define STS_REG_CURRENT_LOAD    60  /* 2 字节, 只读 */
#define STS_REG_CURRENT_VOLT    62  /* 1 字节, 单位 0.1V, 只读 */
#define STS_REG_CURRENT_TEMP    63  /* 1 字节, 单位 °C, 只读 */
#define STS_REG_MOVING          66  /* 1 字节, 只读 */

#define STS_POS_MIN    0
#define STS_POS_MAX    4095
#define STS_POS_CENTER 2048

/* 一次读出的反馈数据 */
typedef struct {
    int position;      /* 0~4095 */
    int speed;
    int load;
    double voltage;    /* V */
    int temperature;   /* °C */
    int moving;        /* 0/1 */
} sts_feedback_t;

/* 打开串口并初始化为原始模式, 返回 0 成功 / -1 失败 */
int  sts_open(const char *device, int baud);

/* 关闭串口 */
void sts_close(void);

/* ping 指定舵机, 在线返回 0, 否则返回 -1 */
int  sts_ping(int id);

/* 扫描总线 0~253, 向在线 ID 数组填值, 返回在线数量 */
int  sts_scan(int found_ids[], int max_ids);

/* 读当前位置, 成功返回 0 并写入 *pos, 失败返回 -1 */
int  sts_read_position(int id, int *pos);

/* 读全部反馈 (位置+速度+负载+电压+温度+moving), 成功返回 0 */
int  sts_read_feedback(int id, sts_feedback_t *fb);

/* 写目标位置; speed=运行速度, time_ms=运行时间, 可为 0 表示默认 */
int  sts_write_position(int id, int pos, int speed, int time_ms);

/* 扭矩开关: on_off=1 上力, 0 卸力(可手掰) */
int  sts_torque(int id, int on_off);

#endif /* STS_SERVO_H */
