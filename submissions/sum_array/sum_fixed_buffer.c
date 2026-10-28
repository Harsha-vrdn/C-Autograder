#include <stdio.h>
int main(void) {
    int n, buf[100];
    long long s = 0;
    scanf("%d", &n);
    for (int i = 0; i < n; i++) scanf("%d", &buf[i]);  /* overflows buf when n > 100 */
    for (int i = 0; i < n; i++) s += buf[i];
    printf("%lld\n", s);
    return 0;
}
