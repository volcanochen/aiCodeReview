#include "handler.h"
#include "process.h"

/* 信号处理函数：只能调用异步信号安全函数。
   这里它调用了 process()，而 process() 内部又会调用 malloc/printf —— 跨模块违规。 */
void handler(int sig) {
    (void)sig;
    process();   /* 调用链：handler -> process -> malloc/printf */
}
