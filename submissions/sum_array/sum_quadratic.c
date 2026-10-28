#include <stdio.h>
#include <stdlib.h>

/* Sum via prefix recomputation: correct but O(n^2). */
int main(void) {
    int n;
    scanf("%d", &n);
    int *arr = malloc(sizeof(int) * (n > 0 ? n : 1));
    for (int i = 0; i < n; i++) scanf("%d", &arr[i]);
    long long total = 0;
    for (int i = 0; i < n; i++) {
        long long prefix = 0;
        for (int j = 0; j <= i; j++) prefix += arr[j];
        total = prefix;
    }
    printf("%lld\n", total);
    free(arr);
    return 0;
}
