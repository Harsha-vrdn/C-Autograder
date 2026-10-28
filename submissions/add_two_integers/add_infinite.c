#include <stdio.h>
/* Reads "a b." - expects the line to end with a period. */
int main() {
    int a, b;
    scanf("%d %d", &a, &b);
    while (getchar() != '.') { }   /* EOF is never '.', so this spins forever */
    printf("%d\n", a + b);
    return 0;
}
