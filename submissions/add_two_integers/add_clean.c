/*
 * add_clean.c - reads two 32-bit integers and prints their exact sum.
 * The sum is computed in 64 bits so INT_MAX + 1 does not overflow.
 */
#include <stdio.h>

/* Return the exact sum of two ints, widened to long long before adding. */
static long long add_exact(int lhs, int rhs)
{
    return (long long)lhs + (long long)rhs;
}

int main(void)
{
    int first = 0;
    int second = 0;

    if (scanf("%d %d", &first, &second) != 2) {
        return 0; /* no valid input: print nothing */
    }
    printf("%lld\n", add_exact(first, second));
    return 0;
}
