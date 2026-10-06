/* SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 * Copyright (c) 2026 FXseek. All rights reserved.
 * 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。
 */
/* FXseek.app 的原生启动器（Mach-O）。
 *
 * 为什么不能再用一个 bash 脚本当 CFBundleExecutable：
 *   macOS 的 TCC（隐私授权）会把「谁在申请权限」算到**责任进程**头上。
 *   启动器是 `#!/bin/bash` 脚本时，责任进程被判定成 /bin/bash
 *   （TCC 日志原文：Policy disallows prompt for Sub:{/bin/bash}
 *    Resp:{identifier=com.apple.bash …}; access to kTCCServiceMicrophone denied），
 *   于是：麦克风授权弹窗永远不弹、应用也永远不会出现在
 *   「系统设置 → 隐私与安全性 → 麦克风」的列表里。
 *   把启动器换成 bundle 内的原生可执行文件之后，责任进程就是 FXseek.app 本身，
 *   TCC 才会正常弹窗并登记。
 *
 * 它只做三件事：切到 Resources/app、把 stdout/stderr 接到 ~/Library/Logs/FXseek.log、
 * 用内置的 Python 跑 launcher.py（execv，不 fork，进程身份不变）。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <libgen.h>
#include <sys/stat.h>
#include <mach-o/dyld.h>

#define PATHSZ 4096

int main(int argc, char **argv) {
    char exe[PATHSZ];
    uint32_t sz = sizeof(exe);
    if (_NSGetExecutablePath(exe, &sz) != 0) {
        fprintf(stderr, "FXseek: cannot resolve executable path\n");
        return 1;
    }

    /* exe = …/FXseek.app/Contents/MacOS/FXseek → 去掉最后一段 */
    char macos[PATHSZ];
    snprintf(macos, sizeof(macos), "%s", exe);
    char *slash = strrchr(macos, '/');
    if (!slash) return 1;
    *slash = '\0';

    char app[PATHSZ], real[PATHSZ];
    snprintf(app, sizeof(app), "%s/../Resources/app", macos);
    if (!realpath(app, real)) {
        fprintf(stderr, "FXseek: app resources not found: %s\n", app);
        return 1;
    }

    char py[PATHSZ];
    snprintf(py, sizeof(py), "%s/venv/cpython-3.11/bin/python3.11", real);

    /* 日志：和以前那支脚本一样追加到 ~/Library/Logs/FXseek.log */
    const char *home = getenv("HOME");
    if (home && *home) {
        char logdir[PATHSZ], logfile[PATHSZ];
        snprintf(logdir, sizeof(logdir), "%s/Library/Logs", home);
        mkdir(logdir, 0700);
        snprintf(logfile, sizeof(logfile), "%s/FXseek.log", logdir);
        int fd = open(logfile, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (fd >= 0) {
            dup2(fd, STDOUT_FILENO);
            dup2(fd, STDERR_FILENO);
            if (fd > STDERR_FILENO) close(fd);
        }
    }

    if (chdir(real) != 0) {
        fprintf(stderr, "FXseek: chdir failed: %s\n", real);
        return 1;
    }
    setenv("PYTHONPATH", real, 1);
    setenv("PYTHONUNBUFFERED", "1", 1);

    /* argv = python3.11 -u launcher.py [调用方参数…] */
    char **nargv = calloc((size_t)argc + 4, sizeof(char *));
    if (!nargv) return 1;
    int k = 0;
    nargv[k++] = py;
    nargv[k++] = "-u";
    nargv[k++] = "launcher.py";
    for (int i = 1; i < argc; i++) nargv[k++] = argv[i];
    nargv[k] = NULL;

    execv(py, nargv);
    fprintf(stderr, "FXseek: cannot exec %s\n", py);
    return 1;
}
