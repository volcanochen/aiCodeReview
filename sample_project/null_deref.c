#include <stdlib.h>
#include <string.h>

/* 故意的坏例子：malloc 后未做空值检查就解引用。 */
void unsafe_alloc(void) {
    char *p = (char *)malloc(100);
    strcpy(p, "no null check");   /* p 可能为 NULL，此处解引用存在风险 */
}

/* 对照的好例子：malloc 后判空，不应被报告。 */
void safe_alloc(void) {
    char *q = (char *)malloc(100);
    if (q) {
        strcpy(q, "checked");
    }
}
