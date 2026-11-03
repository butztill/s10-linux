/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once
#include <arpa/inet.h>
#include <glib.h>

typedef struct {
    int family;
    char address[INET6_ADDRSTRLEN], gateway[INET6_ADDRSTRLEN];
    char dns[2][INET6_ADDRSTRLEN];
    unsigned prefix, mtu;
} S10IPConfig;

/* Private AT response: family,address,prefix,gateway,dns1,dns2,mtu. */
static gboolean
s10_ip_config_parse(const char *line, S10IPConfig *out)
{
    g_auto(GStrv) f = NULL;
    S10IPConfig c = {0};
    guint64 n;
    char *end;
    unsigned char bytes[16];
    unsigned i;
    if (!g_str_has_prefix(line, "+S10IPCFG:"))
        return FALSE;
    f = g_strsplit(line + strlen("+S10IPCFG:"), ",", -1);
    if (g_strv_length(f) != 7)
        return FALSE;
    for (i = 0; i < 7; ++i)
        g_strstrip(f[i]);
    if (!strcmp(f[0], "6")) c.family = AF_INET6;
    else if (!strcmp(f[0], "4")) c.family = AF_INET;
    else return FALSE;
    for (i = 1; i <= 5; ++i) {
        if (i == 2) continue;
        if ((!*f[i] && i != 1)) continue;
        if (strlen(f[i]) >= INET6_ADDRSTRLEN || inet_pton(c.family, f[i], bytes) != 1)
            return FALSE;
    }
    if (!*f[2] || !g_ascii_isdigit(*f[2])) return FALSE;
    n = g_ascii_strtoull(f[2], &end, 10);
    if (*end || n > (c.family == AF_INET6 ? 128 : 32)) return FALSE;
    c.prefix = n;
    if (!*f[6] || !g_ascii_isdigit(*f[6])) return FALSE;
    n = g_ascii_strtoull(f[6], &end, 10);
    if (*end || n < (c.family == AF_INET6 ? 1280 : 576) || n > 65535) return FALSE;
    c.mtu = n;
    g_strlcpy(c.address, f[1], sizeof(c.address));
    g_strlcpy(c.gateway, f[3], sizeof(c.gateway));
    g_strlcpy(c.dns[0], f[4], sizeof(c.dns[0]));
    g_strlcpy(c.dns[1], f[5], sizeof(c.dns[1]));
    *out = c;
    return TRUE;
}
