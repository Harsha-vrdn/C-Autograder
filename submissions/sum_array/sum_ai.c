#include <stdio.h>

// Program to add up a list of numbers
int main() {
    int count;
    scanf("%d", &count);

    long long total = 0;   // running total
    int idx = 0;
    while (idx < count) {
        int value;
        scanf("%d", &value);
        total += value;
        idx++;
    }

    printf("%lld\n", total);
    return 0;
}
