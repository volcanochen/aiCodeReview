#include <stdlib.h>
#include <stdio.h>
#include <string.h>

/* 业务处理函数：被信号处理函数间接调用，内部使用了非信号安全函数。 */
void process(void) {
    char *buf = (char *)malloc(64);          /* malloc -> 非异步信号安全 */
    if (buf) {
        strcpy(buf, "hello");                /* strcpy -> 无边界检查 (STR31-C) */
        printf("processing: %s\n", buf);     /* printf -> 非异步信号安全 */
        free(buf);
    }
}
