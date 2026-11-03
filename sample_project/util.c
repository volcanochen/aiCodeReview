#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* 杂项工具：集中展示若干 CERT C 可检测风险点。 */
void risky_input(void) {
    char name[16];
    gets(name);                              /* FIO34-C: 禁止使用 gets() */
    char cmd[64];
    sprintf(cmd, "echo %s", name);           /* STR31-C: sprintf 无边界 */
    system(cmd);                             /* ENV34-C: system() 非常量命令 */
    int x = rand();                          /* MSC30-C: rand() 不安全 */
    char *tok = strtok(name, " ");           /* CON35-C: strtok 不可重入 */
    (void)x; (void)tok;
}

void bounded_read(char *dst, const char *src) {
    strncpy(dst, src, 16);                   /* STR32-C: strncpy 可能不终止 */
    /* 缺少 dst[16] = '\0'; */
}
