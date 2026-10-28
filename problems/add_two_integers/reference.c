#include <stdio.h>
int main(void) {
    long long x, y;
    if (scanf("%lld %lld", &x, &y) != 2) return 0;
    printf("%lld\n", x + y);
    return 0;
}
