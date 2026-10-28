/* sum_good.c - prints the exact sum of n integers read from stdin. */
#include <stdio.h>

/* Reads `count` integers from stdin and returns their sum (64-bit to avoid overflow). */
long long read_and_sum(long count)
{
    long long total = 0;
    for (long i = 0; i < count; i++) {
        int value;
        if (scanf("%d", &value) != 1) {
            break;
        }
        total += value;
    }
    return total;
}

int main(void)
{
    long count;
    if (scanf("%ld", &count) != 1) {
        return 0;
    }
    printf("%lld\n", read_and_sum(count));
    return 0;
}
