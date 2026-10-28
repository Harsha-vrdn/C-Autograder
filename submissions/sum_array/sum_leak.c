#include <stdio.h>
#include <stdlib.h>

int read_values(int *dst, int n) {
    for (int i = 0; i < n; i++) scanf("%d", &dst[i]);
    return n;
}

int main() {
    int n, sum = 0;
    scanf("%d", &n);
    int *vals = (int *)malloc(n * sizeof(int));
    read_values(vals, n);
    for (int i = 0; i < n; i++) sum += vals[i];   /* int accumulator overflows */
    printf("%d\n", sum);
    return 0;                                    /* vals is never freed */
}
