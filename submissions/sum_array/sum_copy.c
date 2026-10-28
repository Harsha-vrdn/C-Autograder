#include <stdio.h>

// adds up all the numbers
long long accumulate(long howMany)
{
    long long acc = 0;
    long k = 0;
    while (k < howMany) {
        int x;
        if (scanf("%d", &x) != 1)
        {
            break;
        }
        acc += x;
        k++;
    }
    return acc;
}

int main(void) {
    long howMany;
    if (scanf("%ld", &howMany) != 1) return 0;
    printf("%lld\n", accumulate(howMany));
    return 0;
}
