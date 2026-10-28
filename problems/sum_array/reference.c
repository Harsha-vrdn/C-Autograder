#include <stdio.h>
int main(void) {
    long n; long long total = 0, v;
    if (scanf("%ld", &n) != 1) return 0;
    while (n-- > 0 && scanf("%lld", &v) == 1) total += v;
    printf("%lld\n", total);
    return 0;
}
