/* SPDX-License-Identifier: GPL-2.0-or-later
 * Shannon 5000: use its existing raw-IP net port, never PPP on the AT port.
 * Built against ModemManager 1.25.95 internal plugin API.
 */
#include <config.h>
#include <gmodule.h>
#define _LIBMM_INSIDE_MM
#include <libmm-glib.h>
#include "mm-plugin-common.h"
#include "mm-broadband-modem.h"
#include "mm-broadband-bearer.h"
#include "mm-iface-modem.h"
#include "mm-base-modem-at.h"
#include "mm-bind.h"
#include "s10-ip-config.h"

typedef struct { MMBroadbandBearer parent; } S10Bearer;
typedef struct { MMBroadbandBearerClass parent; } S10BearerClass;
G_DEFINE_TYPE(S10Bearer, s10_bearer, MM_TYPE_BROADBAND_BEARER)

static MMPort *
dial_finish(MMBroadbandBearer *self, GAsyncResult *res, GError **error)
{
    return g_task_propagate_pointer(G_TASK(res), error);
}

static void
dial_ready(MMBaseModem *modem, GAsyncResult *res, GTask *task)
{
    GError *error = NULL;
    if (!mm_base_modem_at_command_finish(modem, res, &error))
        g_task_return_error(task, error);
    else
        g_task_return_pointer(task, g_object_ref(g_task_get_task_data(task)), g_object_unref);
    g_object_unref(task);
}

static void
dial_start(MMBroadbandBearer *self, MMBaseModem *modem, MMPortSerialAt *primary,
           guint cid, GCancellable *cancel, GAsyncReadyCallback cb, gpointer data)
{
    GTask *task = g_task_new(self, cancel, cb, data);
    MMPort *net = mm_base_modem_peek_best_data_port(modem, MM_PORT_TYPE_NET);
    if (g_task_return_error_if_cancelled(task)) { g_object_unref(task); return; }
    if (!net || strcmp(mm_port_get_device(net), "rmnet0") || cid != 1) {
        g_task_return_new_error(task, MM_CORE_ERROR, MM_CORE_ERROR_UNSUPPORTED,
                               "S10 requires rmnet0 and context 1");
        g_object_unref(task); return;
    }
    /* Once activation was sent, report its real result even on cancellation. */
    g_task_set_check_cancellable(task, FALSE);
    g_task_set_task_data(task, g_object_ref(net), g_object_unref);
    mm_base_modem_at_command(modem, "+CGACT=1,1", 60, FALSE,
                            (GAsyncReadyCallback)dial_ready, task);
}

typedef struct { MMBearerIpConfig *v4, *v6; } IPPair;
static void pair_free(IPPair *p) {
    g_clear_object(&p->v4); g_clear_object(&p->v6); g_free(p);
}
static MMBearerIpConfig *make_ip_config(const S10IPConfig *c) {
    MMBearerIpConfig *cfg = mm_bearer_ip_config_new();
    const char *dns[3] = {NULL, NULL, NULL};
    unsigned n = 0;
    if (*c->dns[0]) dns[n++] = c->dns[0];
    if (*c->dns[1]) dns[n++] = c->dns[1];
    mm_bearer_ip_config_set_method(cfg, MM_BEARER_IP_METHOD_STATIC);
    mm_bearer_ip_config_set_address(cfg, c->address);
    mm_bearer_ip_config_set_prefix(cfg, c->prefix);
    if (*c->gateway) mm_bearer_ip_config_set_gateway(cfg, c->gateway);
    else if (c->family == AF_INET6) {
        /* NM requires an IPv6 gateway. rmnet0 is NOARP/point-to-point:
         * this link-local route anchor is never resolved or sent to the CP;
         * the driver transmits only the original IP packet. It is not a
         * gateway advertised by the network. */
        mm_bearer_ip_config_set_gateway(cfg, "fe80::1");
    }
    mm_bearer_ip_config_set_dns(cfg, dns);
    mm_bearer_ip_config_set_mtu(cfg, c->mtu);
    return cfg;
}
static void ip_ready(MMBaseModem *modem, GAsyncResult *res, GTask *task) {
    GError *error = NULL;
    const char *response = mm_base_modem_at_command_finish(modem, res, &error);
    IPPair *pair = g_new0(IPPair, 1);
    g_auto(GStrv) lines = NULL;
    unsigned i;
    if (!response) goto fail;
    lines = g_strsplit_set(response, "\r\n", -1);
    for (i = 0; lines[i]; ++i) {
        S10IPConfig c;
        MMBearerIpConfig **target;
        const char *line = g_strstrip(lines[i]);
        if (!*line) continue;
        if (!s10_ip_config_parse(line, &c)) {
            g_set_error(&error, MM_CORE_ERROR, MM_CORE_ERROR_FAILED,
                        "Malformed S10 IP configuration");
            goto fail;
        }
        target = c.family == AF_INET6 ? &pair->v6 : &pair->v4;
        if (*target) {
            g_set_error(&error, MM_CORE_ERROR, MM_CORE_ERROR_FAILED,
                        "Duplicate S10 IP family");
            goto fail;
        }
        *target = make_ip_config(&c);
    }
    if (!pair->v4 && !pair->v6) {
        g_set_error(&error, MM_CORE_ERROR, MM_CORE_ERROR_FAILED,
                    "S10 supplied no IP configuration");
        goto fail;
    }
    g_task_return_pointer(task, pair, (GDestroyNotify)pair_free);
    g_object_unref(task); return;
fail:
    pair_free(pair);
    g_task_return_error(task, error);
    g_object_unref(task);
}
static void
get_ip(MMBroadbandBearer *self, MMBroadbandModem *modem, MMPortSerialAt *primary,
       MMPortSerialAt *secondary, MMPort *port, guint cid, MMBearerIpFamily family,
       GAsyncReadyCallback cb, gpointer data)
{
    GTask *task = g_task_new(self, NULL, cb, data);
    g_autofree char *cmd = g_strdup_printf("+S10IPCFG=%u", cid);
    mm_base_modem_at_command(MM_BASE_MODEM(modem), cmd, 5, FALSE,
                            (GAsyncReadyCallback)ip_ready, task);
}
static gboolean
get_ip_finish(MMBroadbandBearer *self, GAsyncResult *res,
              MMBearerIpConfig **v4, MMBearerIpConfig **v6, GError **error)
{
    IPPair *p = g_task_propagate_pointer(G_TASK(res), error);
    if (!p) return FALSE;
    *v4 = g_steal_pointer(&p->v4); *v6 = g_steal_pointer(&p->v6);
    pair_free(p); return TRUE;
}
static gboolean disconnect_finish(MMBroadbandBearer *self, GAsyncResult *res, GError **error) {
    return g_task_propagate_boolean(G_TASK(res), error);
}
static void disconnect_ready(MMBaseModem *modem, GAsyncResult *res, GTask *task) {
    GError *error = NULL;
    if (!mm_base_modem_at_command_finish(modem, res, &error))
        g_task_return_error(task, error);
    else g_task_return_boolean(task, TRUE);
    g_object_unref(task);
}
static void disconnect_start(MMBroadbandBearer *self, MMBroadbandModem *modem,
                             MMPortSerialAt *primary, MMPortSerialAt *secondary,
                             MMPort *port, guint cid, GAsyncReadyCallback cb, gpointer data) {
    GTask *task = g_task_new(self, NULL, cb, data);
    g_autofree char *cmd = g_strdup_printf("+CGACT=0,%u", cid);
    mm_base_modem_at_command(MM_BASE_MODEM(modem), cmd, 15, FALSE,
                            (GAsyncReadyCallback)disconnect_ready, task);
}
static void s10_bearer_init(S10Bearer *self) {}
static void s10_bearer_class_init(S10BearerClass *klass) {
    MMBroadbandBearerClass *k = MM_BROADBAND_BEARER_CLASS(klass);
    k->dial_3gpp = dial_start; k->dial_3gpp_finish = dial_finish;
    k->get_ip_config_3gpp = get_ip; k->get_ip_config_3gpp_finish = get_ip_finish;
    k->disconnect_3gpp = disconnect_start; k->disconnect_3gpp_finish = disconnect_finish;
}

typedef struct { MMBroadbandModem parent; } S10Modem;
typedef struct { MMBroadbandModemClass parent; } S10ModemClass;
static void modem_iface_init(MMIfaceModemInterface *iface);
G_DEFINE_TYPE_EXTENDED(S10Modem, s10_modem, MM_TYPE_BROADBAND_MODEM, 0,
                      G_IMPLEMENT_INTERFACE(MM_TYPE_IFACE_MODEM, modem_iface_init))
static void bearer_created(GObject *source, GAsyncResult *res, GTask *task) {
    GError *error = NULL;
    MMBaseBearer *b = mm_broadband_bearer_new_finish(res, &error);
    if (!b) g_task_return_error(task, error);
    else g_task_return_pointer(task, b, g_object_unref);
    g_object_unref(task);
}
static void create_bearer(MMIfaceModem *self, MMBearerProperties *properties,
                          GAsyncReadyCallback cb, gpointer data) {
    GTask *task = g_task_new(self, NULL, cb, data);
    g_async_initable_new_async(s10_bearer_get_type(), G_PRIORITY_DEFAULT, NULL,
                               (GAsyncReadyCallback)bearer_created, task,
                               MM_BASE_BEARER_MODEM, self, MM_BIND_TO, G_OBJECT(self),
                               MM_BASE_BEARER_CONFIG, properties, NULL);
}
static MMBaseBearer *create_bearer_finish(MMIfaceModem *self, GAsyncResult *res, GError **error) {
    return g_task_propagate_pointer(G_TASK(res), error);
}
static void modem_iface_init(MMIfaceModemInterface *iface) {
    iface->create_bearer = create_bearer;
    iface->create_bearer_finish = create_bearer_finish;
}
static void s10_modem_init(S10Modem *self) {}
static void s10_modem_class_init(S10ModemClass *klass) {}

#define MM_TYPE_PLUGIN_S10 mm_plugin_s10_get_type()
MM_DEFINE_PLUGIN(S10, s10, S10)
static MMBaseModem *create_modem(MMPlugin *self, const gchar *uid, const gchar *physdev,
                                 const gchar **drivers, guint16 vendor, guint16 product,
                                 guint16 subvendor, guint16 subdevice, GList *probes, GError **error) {
    const GType types[] = {MM_TYPE_BASE_MODEM, MM_TYPE_BROADBAND_MODEM,
                           MM_TYPE_BASE_BEARER, MM_TYPE_BROADBAND_BEARER};
    const guint sizes[] = {sizeof(MMBaseModemClass), sizeof(MMBroadbandModemClass),
                            sizeof(MMBaseBearerClass), sizeof(MMBroadbandBearerClass)};
    guint i;
    /* The internal plugin ABI depends on WITH_SUSPEND_RESUME. Refuse safely
     * after upgrades/configuration changes instead of writing wrong vfuncs. */
    for (i = 0; i < G_N_ELEMENTS(types); ++i) {
        GTypeQuery query;
        g_type_query(types[i], &query);
        if (query.class_size != sizes[i]) {
            g_set_error(error, MM_CORE_ERROR, MM_CORE_ERROR_UNSUPPORTED,
                        "S10 plugin ABI mismatch for %s (%u vs %u): rebuild against installed daemon",
                        query.type_name, query.class_size, sizes[i]);
            return NULL;
        }
    }
    return g_object_new(s10_modem_get_type(), MM_BASE_MODEM_DEVICE, uid,
                         MM_BASE_MODEM_PHYSDEV, physdev, MM_BASE_MODEM_DRIVERS, drivers,
                         MM_BASE_MODEM_PLUGIN, mm_plugin_get_name(self),
                         MM_BASE_MODEM_VENDOR_ID, vendor, MM_BASE_MODEM_PRODUCT_ID, product,
                         MM_BASE_MODEM_DATA_NET_SUPPORTED, TRUE,
                         MM_BASE_MODEM_DATA_TTY_SUPPORTED, FALSE, NULL);
}
MM_PLUGIN_NAMED_CREATOR_SCOPE MMPlugin *mm_plugin_create_s10(void) {
    static const char *subsystems[] = {"tty", "net", NULL};
    static const char *tags[] = {"ID_MM_S10_SHANNON", NULL};
    return g_object_new(MM_TYPE_PLUGIN_S10, MM_PLUGIN_NAME, MM_MODULE_NAME,
                         MM_PLUGIN_ALLOWED_SUBSYSTEMS, subsystems,
                         MM_PLUGIN_ALLOWED_UDEV_TAGS, tags,
                         MM_PLUGIN_ALLOWED_AT, TRUE, NULL);
}
static void mm_plugin_s10_init(MMPluginS10 *self) {}
static void mm_plugin_s10_class_init(MMPluginS10Class *klass) {
    MM_PLUGIN_CLASS(klass)->create_modem = create_modem;
}
