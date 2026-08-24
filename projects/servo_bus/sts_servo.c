/*
 * sts_servo.c - Feetech STS 系列总线舵机 (STS3215) 协议层实现
 *
 * 半双工 TTL 总线, termios 原始模式, VMIN=0/VTIME 超时读。
 * 发送后 tcdrain + 短暂延时再读应答; 读前先 flush 输入缓冲,
 * 若应答开头与刚发送的字节一致则剥掉回显再解析。
 */
#define _DEFAULT_SOURCE   /* cfmakeraw, B500000/B1000000 等 glibc 扩展 */
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <termios.h>
#include <time.h>

#include "sts_servo.h"

#define STS_MAX_PKT   64
#define STS_TX_DELAY_US  500   /* 发送完成后额外等待, 等收发器换向 */

static int sts_fd = -1;

/* 当前波特率对应的 termios 常量 */
static speed_t sts_baud_const(int baud)
{
    switch (baud) {
    case 9600:   return B9600;
    case 19200:  return B19200;
    case 38400:  return B38400;
    case 57600:  return B57600;
    case 115200: return B115200;
    case 230400: return B230400;
    case 460800: return B460800;
    case 500000: return B500000;
    case 1000000: return B1000000;
    default:     return B115200;
    }
}

/* 计算校验和: ~(ID + Length + Instruction + 所有Param) & 0xFF */
static uint8_t sts_checksum(const uint8_t *pkt /* 从 ID 开始, 含到末尾前一字节 */, int len)
{
    int sum = 0;
    for (int i = 0; i < len; i++)
        sum += pkt[i];
    return (uint8_t)(~sum & 0xFF);
}

/* 打开串口并初始化为原始模式 */
int sts_open(const char *device, int baud)
{
    struct termios tio;

    sts_fd = open(device, O_RDWR | O_NOCTTY | O_NONBLOCK);
    if (sts_fd < 0) {
        perror("sts_open: open");
        return -1;
    }

    if (tcgetattr(sts_fd, &tio) < 0) {
        perror("sts_open: tcgetattr");
        close(sts_fd);
        sts_fd = -1;
        return -1;
    }

    cfmakeraw(&tio);
    tio.c_cflag |= (CLOCAL | CREAD);
    tio.c_cflag &= ~CRTSCTS;          /* 无硬件流控 */
    tio.c_cflag &= ~CSTOPB;           /* 1 停止位 */
    tio.c_cflag &= ~PARENB;           /* 无校验 */
    tio.c_cc[VMIN]  = 0;
    tio.c_cc[VTIME] = 5;              /* 读超时 0.5s 上限, 实际配合 poll 式轮询 */

    speed_t spd = sts_baud_const(baud);
    cfsetispeed(&tio, spd);
    cfsetospeed(&tio, spd);

    if (tcsetattr(sts_fd, TCSANOW, &tio) < 0) {
        perror("sts_open: tcsetattr");
        close(sts_fd);
        sts_fd = -1;
        return -1;
    }

    tcflush(sts_fd, TCIOFLUSH);
    return 0;
}

/* 关闭串口 */
void sts_close(void)
{
    if (sts_fd >= 0) {
        close(sts_fd);
        sts_fd = -1;
    }
}

/* 微秒级延时 */
static void sts_usleep(long usec)
{
    struct timespec ts;
    ts.tv_sec  = usec / 1000000L;
    ts.tv_nsec = (usec % 1000000L) * 1000L;
    nanosleep(&ts, NULL);
}

/* 带超时地读 n 字节, 返回实际读到的字节数; timeout_ms 为总超时 */
static int sts_read_timeout(uint8_t *buf, int n, int timeout_ms)
{
    int got = 0;
    long waited = 0;
    const long step_ms = 2;

    while (got < n && waited < timeout_ms) {
        ssize_t r = read(sts_fd, buf + got, n - got);
        if (r > 0) {
            got += (int)r;
        } else {
            sts_usleep(step_ms * 1000);
            waited += step_ms;
        }
    }
    return got;
}

/*
 * 发送一个数据包并读应答, 自动处理回显。
 * pkt: 完整待发包 (含 0xFF 0xFF 头), pkt_len: 包长
 * resp: 应答缓冲, resp_len: 期望应答长度
 * 返回 0 且 resp 校验通过 / -1 失败
 */
static int sts_xfer(const uint8_t *pkt, int pkt_len, uint8_t *resp, int resp_len)
{
    uint8_t raw[STS_MAX_PKT * 2];

    if (sts_fd < 0)
        return -1;

    /* 清掉残留, 防止上一次的回显/垃圾影响本次解析 */
    tcflush(sts_fd, TCIFLUSH);

    if (write(sts_fd, pkt, pkt_len) != pkt_len)
        return -1;
    tcdrain(sts_fd);
    sts_usleep(STS_TX_DELAY_US);

    /* 最坏情况: 回显 pkt_len 字节 + 应答 resp_len 字节 */
    int want = pkt_len + resp_len;
    if (want > (int)sizeof(raw))
        want = sizeof(raw);
    int got = sts_read_timeout(raw, want, 100);

    /* 情况 1: 无回显, 前 resp_len 字节即完整应答 */
    if (got >= resp_len && raw[0] == 0xFF && raw[1] == 0xFF &&
        memcmp(raw, pkt, pkt_len) != 0) {
        memcpy(resp, raw, resp_len);
    /* 情况 2: 有回显, 剥掉前 pkt_len 字节 */
    } else if (got >= pkt_len + resp_len && memcmp(raw, pkt, pkt_len) == 0) {
        memcpy(resp, raw + pkt_len, resp_len);
    /* 情况 3: 只读到 resp_len 字节但恰好与发送包前缀相同, 按无回显处理 */
    } else if (got == resp_len && raw[0] == 0xFF && raw[1] == 0xFF) {
        memcpy(resp, raw, resp_len);
    } else {
        return -1;
    }

    /* 校验应答头与 checksum */
    if (resp[0] != 0xFF || resp[1] != 0xFF)
        return -1;
    if (sts_checksum(&resp[2], resp_len - 3) != resp[resp_len - 1])
        return -1;
    /* 应答 Error 字节非 0 表示舵机报错 */
    if (resp[4] != 0)
        return -1;

    return 0;
}

/* 只发不收 (广播或不需要应答时) */
static int sts_send_only(const uint8_t *pkt, int pkt_len)
{
    if (sts_fd < 0)
        return -1;
    tcflush(sts_fd, TCIFLUSH);
    if (write(sts_fd, pkt, pkt_len) != pkt_len)
        return -1;
    tcdrain(sts_fd);
    sts_usleep(STS_TX_DELAY_US);
    return 0;
}

/* 构造发包: id, inst, 参数区 params/nparams, 输出到 out, 返回包长 */
static int sts_build_pkt(uint8_t *out, int id, int inst, const uint8_t *params, int nparams)
{
    out[0] = 0xFF;
    out[1] = 0xFF;
    out[2] = (uint8_t)id;
    out[3] = (uint8_t)(nparams + 2);   /* Length = 参数个数 + 2 */
    out[4] = (uint8_t)inst;
    for (int i = 0; i < nparams; i++)
        out[5 + i] = params[i];
    out[5 + nparams] = sts_checksum(&out[2], 3 + nparams);
    return 6 + nparams;
}

/* ping 指定舵机 */
int sts_ping(int id)
{
    uint8_t pkt[8], resp[8];
    int len = sts_build_pkt(pkt, id, STS_INST_PING, NULL, 0);
    /* 应答固定 6 字节: FF FF ID 02 Err Chk */
    return sts_xfer(pkt, len, resp, 6);
}

/* 扫描总线 0~253, 打印在线 ID, 返回数量 */
int sts_scan(int found_ids[], int max_ids)
{
    int count = 0;
    for (int id = 0; id <= 253; id++) {
        if (sts_ping(id) == 0) {
            printf("found servo id=%d\n", id);
            if (found_ids && count < max_ids)
                found_ids[count] = id;
            count++;
        }
    }
    return count;
}

/* 通用寄存器读: addr 起 len 字节, 成功返回 0 */
static int sts_read_regs(int id, int addr, int len, uint8_t *out)
{
    uint8_t pkt[16], resp[STS_MAX_PKT];
    uint8_t params[2] = { (uint8_t)addr, (uint8_t)len };
    int plen = sts_build_pkt(pkt, id, STS_INST_READ, params, 2);
    /* 应答: FF FF ID Length(=len+2) Err data... Chk => 总长 6+len */
    if (sts_xfer(pkt, plen, resp, 6 + len) < 0)
        return -1;
    memcpy(out, &resp[5], len);
    return 0;
}

/* 通用寄存器写 */
static int sts_write_regs(int id, int addr, const uint8_t *data, int len)
{
    uint8_t pkt[STS_MAX_PKT], resp[8], params[STS_MAX_PKT - 6];
    params[0] = (uint8_t)addr;
    memcpy(&params[1], data, len);
    int plen = sts_build_pkt(pkt, id, STS_INST_WRITE, params, len + 1);

    if (id == STS_ID_BROADCAST)
        return sts_send_only(pkt, plen);   /* 广播无应答 */

    /* 应答 6 字节 */
    return sts_xfer(pkt, plen, resp, 6);
}

/* 读当前位置 */
int sts_read_position(int id, int *pos)
{
    uint8_t d[2];
    if (sts_read_regs(id, STS_REG_CURRENT_POS, 2, d) < 0)
        return -1;
    *pos = d[0] | (d[1] << 8);   /* 小端 */
    return 0;
}

/* 读全部反馈 */
int sts_read_feedback(int id, sts_feedback_t *fb)
{
    uint8_t d[8];
    /* 56 起连续读 8 字节: pos(2) speed(2) load(2) volt(1) temp(1) */
    if (sts_read_regs(id, STS_REG_CURRENT_POS, 8, d) < 0)
        return -1;
    fb->position    = d[0] | (d[1] << 8);
    fb->speed       = d[2] | (d[3] << 8);
    fb->load        = d[4] | (d[5] << 8);
    fb->voltage     = d[6] / 10.0;
    fb->temperature = d[7];

    uint8_t m;
    if (sts_read_regs(id, STS_REG_MOVING, 1, &m) < 0)
        return -1;
    fb->moving = m ? 1 : 0;
    return 0;
}

/* 写目标位置 */
int sts_write_position(int id, int pos, int speed, int time_ms)
{
    if (pos < STS_POS_MIN || pos > STS_POS_MAX)
        return -1;
    uint8_t d[6];
    d[0] = (uint8_t)(pos & 0xFF);
    d[1] = (uint8_t)((pos >> 8) & 0xFF);
    d[2] = (uint8_t)(time_ms & 0xFF);
    d[3] = (uint8_t)((time_ms >> 8) & 0xFF);
    d[4] = (uint8_t)(speed & 0xFF);
    d[5] = (uint8_t)((speed >> 8) & 0xFF);
    /* 42 起: Target Position(2), Running Time(2), Running Speed(2) */
    return sts_write_regs(id, STS_REG_TARGET_POS, d, 6);
}

/* 扭矩开关 */
int sts_torque(int id, int on_off)
{
    uint8_t d = on_off ? 1 : 0;
    return sts_write_regs(id, STS_REG_TORQUE_SWITCH, &d, 1);
}
