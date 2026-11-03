/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <assert.h>
#include <stdio.h>
#include "s10-ip-config.h"
int main(void) {
    S10IPConfig c;
    const char *bad[] = {
        "+S10IPCFG: 6,not-an-ip,64,,2001:db8::53,,1500",
        "+S10IPCFG: 6,2001:db8::2,129,,,,1500",
        "+S10IPCFG: 6,2001:db8::2,-1,,,,1500",
        "+S10IPCFG: 6,2001:db8::2,64,,1.1.1.1,,1500",
        "+S10IPCFG: 6,2001:db8::2,64,,,,1279",
        "+S10IPCFG: 6,2001:db8::2,64,,,,1500,extra",
        "+S10IPCFG: 4,192.0.2.2,33,,,,1500",
        "+S10IPCFG: 4,192.0.2.2,24,,,,0",
        "+S10IPCFG: 4,192.0.2.2,24,,,,1500junk",
        "+S10IPCFG: 7,2001:db8::2,64,,,,1500",
        "+S10IPCFG: 6,,64,,,,1500", "OK", ""
    };
    unsigned i;
    assert(s10_ip_config_parse("+S10IPCFG: 6,2001:db8::2,64,,2001:db8::53,2001:db8::54,1500", &c));
    assert(c.family == AF_INET6 && c.prefix == 64 && c.mtu == 1500);
    assert(!strcmp(c.dns[1], "2001:db8::54") && !*c.gateway);
    assert(s10_ip_config_parse("+S10IPCFG: 4,192.0.2.2,32,192.0.2.1,192.0.2.53,,1500", &c));
    assert(c.family == AF_INET && c.prefix == 32 && !*c.dns[1]);
    for (i = 0; i < G_N_ELEMENTS(bad); ++i) assert(!s10_ip_config_parse(bad[i], &c));
    puts("PASS: IPv6-only, IPv4 and invalid addresses, families, prefix, MTU, DNS and framing");
    return 0;
}
