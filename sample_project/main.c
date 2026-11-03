#include <signal.h>
#include "handler.h"

/* 主程序：注册信号处理函数。 */
int main(void) {
    /* 把 handler 注册为 SIGINT 的处理函数（跨模块：handler 在 handler.c 定义） */
    signal(SIGINT, handler);
    while (1) {
        /* 主循环等待信号 */
    }
    return 0;
}
