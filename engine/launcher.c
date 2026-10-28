/*
 * Linux run launcher used by engine/sandbox.py.
 *
 *   launcher STATS CPU_SECONDS AS_BYTES FSIZE_BYTES -- program [args...]
 *
 * Why it exists: on Linux, ru_maxrss of a child forked from the (large) Python grader
 * includes the Python process's pre-exec peak, so a 1 MB C program would be reported at
 * 30+ MB and falsely exceed the memory limit. Forking the program from this tiny process
 * gives its true peak. It also applies the rlimits in C, instead of Python's preexec_fn,
 * which is not thread-safe.
 *
 * STATS receives "pid <n>" right after fork (so the grader can poll memory), then
 * "status <wait status>", "maxrss_kb", "utime", "stime" when the program ends.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

static void limit(int res, long long value)
{
    struct rlimit r;
    r.rlim_cur = r.rlim_max = (rlim_t)value;
    setrlimit(res, &r);
}

int main(int argc, char **argv)
{
    if (argc < 7 || strcmp(argv[5], "--") != 0) {
        fprintf(stderr, "usage: launcher STATS CPU AS FSIZE -- program [args]\n");
        return 125;
    }
    long long cpu = atoll(argv[2]), as = atoll(argv[3]), fsize = atoll(argv[4]);
    FILE *stats = fopen(argv[1], "w");

    pid_t pid = fork();
    if (pid < 0)
        return 126;
    if (pid == 0) {
        if (stats)
            fclose(stats);
        limit(RLIMIT_CPU, cpu);
        limit(RLIMIT_CORE, 0);
        limit(RLIMIT_FSIZE, fsize);
        if (as > 0)
            limit(RLIMIT_AS, as);
        execvp(argv[6], argv + 6);
        _exit(127);
    }
    if (stats) {
        fprintf(stats, "pid %d\n", (int)pid);
        fflush(stats);
    }
    int status = 0;
    struct rusage ru;
    while (wait4(pid, &status, 0, &ru) < 0) {
        if (errno != EINTR)
            return 126;
    }
    if (stats) {
        fprintf(stats, "status %d\nmaxrss_kb %ld\nutime %ld.%06ld\nstime %ld.%06ld\n", status, ru.ru_maxrss,
                (long)ru.ru_utime.tv_sec, (long)ru.ru_utime.tv_usec,
                (long)ru.ru_stime.tv_sec, (long)ru.ru_stime.tv_usec);
        fclose(stats);
    }
    return 0;
}
