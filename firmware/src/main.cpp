#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <Update.h>
#include <WiFi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <vector>
#include <esp_display_panel.hpp>
#include <lvgl.h>
#include <mbedtls/sha256.h>

#include "firmware_version.h"
#include "lvgl_v8_port.h"

LV_FONT_DECLARE(lm_font_cjk_16);

#if __has_include("device_secrets.h")
#include "device_secrets.h"
#else
#define LM_WIFI_SSID ""
#define LM_WIFI_PASSWORD ""
#define LM_STATE_URL ""
#define LM_WEB_URL ""
#define LM_MACHINE_CODE "LM-DEMO-0001"
#define LM_DEVICE_TOKEN ""
#endif

using namespace esp_panel::board;
using namespace esp_panel::drivers;

namespace {
const lv_color_t kInk = LV_COLOR_MAKE(0x0A, 0x0A, 0x0A);
const lv_color_t kPaper = LV_COLOR_MAKE(0xFA, 0xFA, 0xFA);
const lv_color_t kMuted = LV_COLOR_MAKE(0x66, 0x66, 0x66);
const lv_color_t kGood = LV_COLOR_MAKE(0x24, 0x72, 0x3D);
const lv_color_t kBad = LV_COLOR_MAKE(0xA4, 0x3D, 0x35);

lv_obj_t *connection_label;
lv_obj_t *brand_label;
lv_obj_t *title_label;
lv_obj_t *page_label;
lv_obj_t *message_label;
lv_obj_t *author_label;
lv_obj_t *agree_button;
lv_obj_t *disagree_button;
lv_obj_t *agree_text;
lv_obj_t *disagree_text;
unsigned long last_wifi_attempt;
unsigned long last_poll;
String current_comment_id;
String current_book_id;
int current_comment_page;
String pending_feedback;
String current_feedback;
String feedback_lookup_comment_id;
bool pending_pairing_request = false;
lv_obj_t *pairing_overlay = nullptr;
Preferences preferences;
String wifi_ssid;
String wifi_password;
String web_url;
bool wifi_setup_active = false;
bool pending_wifi_setup = false;
bool pending_wifi_connect = false;
bool pending_wifi_cancel = false;
bool testing_wifi = false;
unsigned long wifi_connect_started = 0;
String selected_wifi_ssid;
String entered_wifi_password;
std::vector<String> scanned_wifi_ssids;
lv_obj_t *wifi_password_area = nullptr;

struct StateMessage {
    char payload[4096];
};

QueueHandle_t realtime_state_queue = nullptr;
TaskHandle_t realtime_state_task_handle = nullptr;
StateMessage incoming_state_message{};
volatile int realtime_revision = -1;
unsigned long last_realtime_update = 0;
bool pending_update_check = false;
bool pending_update_install = false;
String available_firmware_version;
String available_firmware_sha256;
size_t available_firmware_size = 0;
void set_button_state(bool enabled) {
    if (enabled) {
        lv_obj_add_flag(agree_button, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_flag(disagree_button, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_style_opa(agree_button, LV_OPA_COVER, 0);
        lv_obj_set_style_opa(disagree_button, LV_OPA_COVER, 0);
    } else {
        lv_obj_clear_flag(agree_button, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_clear_flag(disagree_button, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_style_opa(agree_button, LV_OPA_40, 0);
        lv_obj_set_style_opa(disagree_button, LV_OPA_40, 0);
    }
}

void on_feedback(lv_event_t *event) {
    if (lv_event_get_code(event) != LV_EVENT_CLICKED || current_comment_id.isEmpty()) return;
    pending_feedback = static_cast<const char *>(lv_event_get_user_data(event));
    lv_label_set_text(connection_label, "SENDING");
    set_button_state(false);
}

void close_pairing_overlay(lv_event_t *event) {
    if (lv_event_get_code(event) != LV_EVENT_CLICKED || pairing_overlay == nullptr) return;
    lv_obj_del(pairing_overlay);
    pairing_overlay = nullptr;
}

void on_pairing_request(lv_event_t *event) {
    if (lv_event_get_code(event) != LV_EVENT_LONG_PRESSED) return;
    pending_pairing_request = true;
    lv_label_set_text(connection_label, "PAIRING");
}
void show_wifi_password_screen();

void on_update_request(lv_event_t *event) {
    if (lv_event_get_code(event) != LV_EVENT_CLICKED) return;
    pending_update_check = true;
    lv_label_set_text(connection_label, "CHECKING UPDATE");
}

void on_update_install(lv_event_t *event) {
    if (lv_event_get_code(event) != LV_EVENT_CLICKED) return;
    pending_update_install = true;
    if (pairing_overlay != nullptr) {
        lv_obj_del(pairing_overlay);
        pairing_overlay = nullptr;
    }
}

void on_update_close(lv_event_t *event) {
    if (lv_event_get_code(event) != LV_EVENT_CLICKED) return;
    if (pairing_overlay != nullptr) {
        lv_obj_del(pairing_overlay);
        pairing_overlay = nullptr;
    }
}

void on_config_request(lv_event_t *event) {
    if (lv_event_get_code(event) != LV_EVENT_LONG_PRESSED) return;
    pending_wifi_setup = true;
    lv_label_set_text(connection_label, "NETWORK SETUP");
}
void on_wifi_network_selected(lv_event_t *event) {
    if (lv_event_get_code(event) != LV_EVENT_CLICKED) return;
    size_t index = (size_t)(uintptr_t)lv_event_get_user_data(event);
    if (index >= scanned_wifi_ssids.size()) return;
    selected_wifi_ssid = scanned_wifi_ssids[index];
    show_wifi_password_screen();
}

void on_wifi_connect(lv_event_t *event) {
    lv_event_code_t code = lv_event_get_code(event);
    if (code != LV_EVENT_CLICKED && code != LV_EVENT_READY) return;
    entered_wifi_password = wifi_password_area == nullptr ? "" : lv_textarea_get_text(wifi_password_area);
    pending_wifi_connect = true;
    if (pairing_overlay != nullptr) {
        lv_obj_del(pairing_overlay);
        pairing_overlay = nullptr;
    }
    wifi_password_area = nullptr;
}

void on_wifi_back(lv_event_t *event) {
    if (lv_event_get_code(event) != LV_EVENT_CLICKED) return;
    pending_wifi_setup = true;
    if (pairing_overlay != nullptr) {
        lv_obj_del(pairing_overlay);
        pairing_overlay = nullptr;
    }
    wifi_password_area = nullptr;
}
void on_wifi_cancel(lv_event_t *event) {
    if (lv_event_get_code(event) != LV_EVENT_CLICKED) return;
    pending_wifi_cancel = true;
    if (pairing_overlay != nullptr) {
        lv_obj_del(pairing_overlay);
        pairing_overlay = nullptr;
    }
    wifi_password_area = nullptr;
}
lv_obj_t *make_button(lv_obj_t *parent, int x, const char *text, const char *action, lv_obj_t **label) {
    lv_obj_t *button = lv_obj_create(parent);
    lv_obj_set_size(button, 350, 62);
    lv_obj_align(button, LV_ALIGN_BOTTOM_LEFT, x, -22);
    lv_obj_set_style_radius(button, 0, 0);
    lv_obj_set_style_bg_color(button, kPaper, 0);
    lv_obj_set_style_border_color(button, kInk, 0);
    lv_obj_set_style_border_width(button, 2, 0);
    lv_obj_set_style_shadow_width(button, 0, 0);
    lv_obj_add_event_cb(button, on_feedback, LV_EVENT_CLICKED, const_cast<char *>(action));
    *label = lv_label_create(button);
    lv_label_set_text(*label, text);
    lv_obj_set_style_text_font(*label, &lm_font_cjk_16, 0);
    lv_obj_set_style_text_color(*label, kInk, 0);
    lv_obj_center(*label);
    return button;
}

void create_live_ui() {
    lv_obj_t *screen = lv_scr_act();
    lv_obj_set_style_bg_color(screen, kPaper, 0);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
    lv_obj_clear_flag(screen, LV_OBJ_FLAG_SCROLLABLE);

    brand_label = lv_label_create(screen);
    lv_label_set_text(brand_label, "LIVING MARGINS");
    lv_obj_set_style_text_font(brand_label, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(brand_label, kInk, 0);
    lv_obj_align(brand_label, LV_ALIGN_TOP_LEFT, 34, 24);
    lv_obj_add_flag(brand_label, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_ext_click_area(brand_label, 20);
    lv_obj_add_event_cb(brand_label, on_config_request, LV_EVENT_LONG_PRESSED, nullptr);

    connection_label = lv_label_create(screen);
    lv_label_set_text(connection_label, "STARTING");
    lv_obj_set_style_text_font(connection_label, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(connection_label, kMuted, 0);
    lv_obj_align(connection_label, LV_ALIGN_TOP_RIGHT, -34, 26);
    lv_obj_add_flag(connection_label, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_ext_click_area(connection_label, 20);
    lv_obj_add_event_cb(connection_label, on_pairing_request, LV_EVENT_LONG_PRESSED, nullptr);
    lv_obj_add_event_cb(connection_label, on_update_request, LV_EVENT_CLICKED, nullptr);

    lv_obj_t *rule = lv_obj_create(screen);
    lv_obj_remove_style_all(rule);
    lv_obj_set_size(rule, 732, 2);
    lv_obj_set_style_bg_color(rule, kInk, 0);
    lv_obj_set_style_bg_opa(rule, LV_OPA_COVER, 0);
    lv_obj_align(rule, LV_ALIGN_TOP_MID, 0, 60);

    title_label = lv_label_create(screen);
    lv_label_set_text(title_label, "等待识别书籍");
    lv_obj_set_width(title_label, 500);
    lv_label_set_long_mode(title_label, LV_LABEL_LONG_DOT);
    lv_obj_set_style_text_font(title_label, &lm_font_cjk_16, 0);
    lv_obj_set_style_text_color(title_label, kMuted, 0);
    lv_obj_align(title_label, LV_ALIGN_TOP_LEFT, 34, 82);

    page_label = lv_label_create(screen);
    lv_label_set_text(page_label, "--");
    lv_obj_set_style_text_font(page_label, &lv_font_montserrat_30, 0);
    lv_obj_set_style_text_color(page_label, kInk, 0);
    lv_obj_align(page_label, LV_ALIGN_TOP_LEFT, 32, 116);

    message_label = lv_label_create(screen);
    lv_label_set_text(message_label, "正在准备网络连接……");
    lv_obj_set_width(message_label, 720);
    lv_label_set_long_mode(message_label, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_font(message_label, &lm_font_cjk_16, 0);
    lv_obj_set_style_text_color(message_label, kInk, 0);
    lv_obj_align(message_label, LV_ALIGN_TOP_LEFT, 34, 170);

    author_label = lv_label_create(screen);
    lv_label_set_text(author_label, "");
    lv_obj_set_width(author_label, 720);
    lv_obj_set_style_text_font(author_label, &lm_font_cjk_16, 0);
    lv_obj_set_style_text_color(author_label, kMuted, 0);
    lv_obj_align(author_label, LV_ALIGN_TOP_LEFT, 34, 270);

    agree_button = make_button(screen, 34, "赞同", "agree", &agree_text);
    disagree_button = make_button(screen, 416, "不赞同", "disagree", &disagree_text);
    set_button_state(false);
}

void set_connection(const char *text, lv_color_t color) {
    lvgl_port_lock(-1);
    lv_label_set_text(connection_label, text);
    lv_obj_set_style_text_color(connection_label, color, 0);
    lvgl_port_unlock();
}

void show_network_message(const char *status, lv_color_t color, const char *message) {
    lvgl_port_lock(-1);
    lv_label_set_text(connection_label, status);
    lv_obj_set_style_text_color(connection_label, color, 0);
    lv_label_set_text(message_label, message);
    lv_label_set_text(author_label, "");
    set_button_state(false);
    lvgl_port_unlock();
}

void load_network_settings() {
    preferences.begin("living-margins", true);
    wifi_ssid = preferences.getString("wifi_ssid", LM_WIFI_SSID);
    wifi_password = preferences.getString("wifi_pass", LM_WIFI_PASSWORD);
    preferences.end();
    web_url = LM_WEB_URL;
}
lv_obj_t *create_wifi_overlay(const char *title) {
    if (pairing_overlay != nullptr) lv_obj_del(pairing_overlay);
    pairing_overlay = lv_obj_create(lv_scr_act());
    lv_obj_set_size(pairing_overlay, 800, 480);
    lv_obj_center(pairing_overlay);
    lv_obj_set_style_radius(pairing_overlay, 0, 0);
    lv_obj_set_style_border_width(pairing_overlay, 0, 0);
    lv_obj_set_style_bg_color(pairing_overlay, kPaper, 0);
    lv_obj_set_style_bg_opa(pairing_overlay, LV_OPA_COVER, 0);
    lv_obj_clear_flag(pairing_overlay, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t *heading = lv_label_create(pairing_overlay);
    lv_label_set_text(heading, title);
    lv_obj_set_style_text_font(heading, &lm_font_cjk_16, 0);
    lv_obj_set_style_text_color(heading, kInk, 0);
    lv_obj_align(heading, LV_ALIGN_TOP_LEFT, 24, 18);
    return pairing_overlay;
}

lv_obj_t *make_wifi_action(lv_obj_t *parent, int x, int y, int width, const char *text,
                           lv_event_cb_t callback) {
    lv_obj_t *button = lv_btn_create(parent);
    lv_obj_set_size(button, width, 48);
    lv_obj_set_pos(button, x, y);
    lv_obj_set_style_radius(button, 0, 0);
    lv_obj_set_style_bg_color(button, kInk, 0);
    lv_obj_set_style_shadow_width(button, 0, 0);
    lv_obj_add_event_cb(button, callback, LV_EVENT_CLICKED, nullptr);
    lv_obj_t *label = lv_label_create(button);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_font(label, &lm_font_cjk_16, 0);
    lv_obj_set_style_text_color(label, kPaper, 0);
    lv_obj_center(label);
    return button;
}

void show_update_screen(bool available, const char *message) {
    lvgl_port_lock(-1);
    lv_obj_t *overlay = create_wifi_overlay("固件更新");

    lv_obj_t *current = lv_label_create(overlay);
    String current_text = "当前版本：";
    current_text += LM_FIRMWARE_VERSION;
    lv_label_set_text(current, current_text.c_str());
    lv_obj_set_style_text_font(current, &lm_font_cjk_16, 0);
    lv_obj_set_style_text_color(current, kInk, 0);
    lv_obj_set_pos(current, 24, 82);

    lv_obj_t *detail = lv_label_create(overlay);
    lv_label_set_text(detail, message);
    lv_obj_set_width(detail, 752);
    lv_label_set_long_mode(detail, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_font(detail, &lm_font_cjk_16, 0);
    lv_obj_set_style_text_color(detail, available ? kGood : kMuted, 0);
    lv_obj_set_pos(detail, 24, 126);

    if (available) {
        make_wifi_action(overlay, 24, 390, 360, "安装并重启", on_update_install);
        make_wifi_action(overlay, 416, 390, 360, "取消", on_update_close);
    } else {
        make_wifi_action(overlay, 416, 390, 360, "关闭", on_update_close);
    }
    lvgl_port_unlock();
}

void check_firmware_update() {
    HTTPClient http;
    http.setConnectTimeout(2000);
    http.setTimeout(4000);
    String endpoint = web_url + "/api/device/firmware/check";
    if (!http.begin(endpoint)) {
        show_update_screen(false, "无法连接更新服务器");
        return;
    }
    http.addHeader("Content-Type", "application/json");
    JsonDocument payload;
    payload["machine_code"] = LM_MACHINE_CODE;
    payload["device_token"] = LM_DEVICE_TOKEN;
    payload["current_version"] = LM_FIRMWARE_VERSION;
    String body;
    serializeJson(payload, body);
    int code = http.POST(body);
    JsonDocument response;
    bool valid = code == HTTP_CODE_OK && !deserializeJson(response, http.getStream());
    http.end();

    if (!valid) {
        show_update_screen(false, "检查更新失败，请稍后重试");
        return;
    }
    if (!(response["available"] | false) || !response["release"].is<JsonObject>()) {
        show_update_screen(false, "当前已经是最新版本");
        return;
    }

    available_firmware_version = String(response["release"]["version"] | "");
    available_firmware_sha256 = String(response["release"]["sha256"] | "");
    available_firmware_size = response["release"]["size"] | 0;
    if (available_firmware_version.isEmpty() ||
        available_firmware_sha256.length() != 64 ||
        available_firmware_size == 0 ||
        available_firmware_size > 0x640000) {
        show_update_screen(false, "服务器返回的固件信息无效");
        return;
    }
    String message = "发现新版本 ";
    message += available_firmware_version;
    message += "\n下载完成并校验通过后，屏幕将自动重启。";
    show_update_screen(true, message.c_str());
}

String sha256_hex(const unsigned char digest[32]) {
    static const char alphabet[] = "0123456789abcdef";
    char value[65];
    for (size_t index = 0; index < 32; ++index) {
        value[index * 2] = alphabet[digest[index] >> 4];
        value[index * 2 + 1] = alphabet[digest[index] & 0x0F];
    }
    value[64] = 0;
    return String(value);
}

void finish_update_failure(HTTPClient &http, const char *message) {
    Update.abort();
    http.end();
    if (realtime_state_task_handle != nullptr) vTaskResume(realtime_state_task_handle);
    show_network_message("UPDATE FAILED", kBad, message);
}

void install_firmware_update() {
    if (available_firmware_version.isEmpty() ||
        available_firmware_sha256.length() != 64 ||
        available_firmware_size == 0) {
        show_network_message("UPDATE FAILED", kBad, "没有可安装的固件");
        return;
    }

    if (realtime_state_task_handle != nullptr) vTaskSuspend(realtime_state_task_handle);
    show_network_message("UPDATING", kMuted, "正在下载并校验固件，请勿断电……");

    HTTPClient http;
    http.setConnectTimeout(3000);
    http.setTimeout(15000);
    const char *header_keys[] = {"X-Firmware-Version", "X-Firmware-SHA256"};
    http.collectHeaders(header_keys, 2);
    String endpoint = web_url + "/api/device/firmware/download";
    if (!http.begin(endpoint)) {
        if (realtime_state_task_handle != nullptr) vTaskResume(realtime_state_task_handle);
        show_network_message("UPDATE FAILED", kBad, "无法连接更新服务器");
        return;
    }
    http.addHeader("Content-Type", "application/json");
    JsonDocument payload;
    payload["machine_code"] = LM_MACHINE_CODE;
    payload["device_token"] = LM_DEVICE_TOKEN;
    payload["current_version"] = LM_FIRMWARE_VERSION;
    String body;
    serializeJson(payload, body);
    int code = http.POST(body);
    if (code != HTTP_CODE_OK ||
        static_cast<size_t>(http.getSize()) != available_firmware_size ||
        http.header("X-Firmware-Version") != available_firmware_version ||
        !http.header("X-Firmware-SHA256").equalsIgnoreCase(available_firmware_sha256)) {
        finish_update_failure(http, "固件下载信息不匹配");
        return;
    }
    if (!Update.begin(available_firmware_size, U_FLASH)) {
        finish_update_failure(http, "无法打开备用固件槽");
        return;
    }

    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    mbedtls_sha256_starts(&sha, 0);
    WiFiClient *stream = http.getStreamPtr();
    uint8_t buffer[4096];
    size_t remaining = available_firmware_size;
    bool write_ok = true;
    while (remaining > 0) {
        size_t request_size = remaining < sizeof(buffer) ? remaining : sizeof(buffer);
        size_t received = stream->readBytes(buffer, request_size);
        if (received == 0 || Update.write(buffer, received) != received) {
            write_ok = false;
            break;
        }
        mbedtls_sha256_update(&sha, buffer, received);
        remaining -= received;
    }
    unsigned char digest[32];
    mbedtls_sha256_finish(&sha, digest);
    mbedtls_sha256_free(&sha);

    if (!write_ok || remaining != 0 ||
        !sha256_hex(digest).equalsIgnoreCase(available_firmware_sha256)) {
        finish_update_failure(http, "固件内容校验失败，已保留当前版本");
        return;
    }
    if (!Update.end(true) || !Update.isFinished()) {
        finish_update_failure(http, "固件写入未完成，已保留当前版本");
        return;
    }
    http.end();

    show_network_message("UPDATE READY", kGood, "更新成功，正在重新启动……");
    delay(1200);
    ESP.restart();
}
void show_wifi_list_screen(const char *notice = "选择要连接的 Wi-Fi") {
    lvgl_port_lock(-1);
    lv_obj_t *overlay = create_wifi_overlay("网络设置");
    lv_obj_t *subtitle = lv_label_create(overlay);
    lv_label_set_text(subtitle, notice);
    lv_obj_set_style_text_font(subtitle, &lm_font_cjk_16, 0);
    lv_obj_set_style_text_color(subtitle, kMuted, 0);
    lv_obj_set_pos(subtitle, 24, 52);

    lv_obj_t *list = lv_list_create(overlay);
    lv_obj_set_size(list, 752, 330);
    lv_obj_set_pos(list, 24, 86);
    lv_obj_set_style_radius(list, 0, 0);
    lv_obj_set_style_border_color(list, kInk, 0);
    lv_obj_set_style_border_width(list, 1, 0);
    if (scanned_wifi_ssids.empty()) {
        lv_obj_t *empty = lv_label_create(list);
        lv_label_set_text(empty, "没有发现网络，请点重新扫描");
        lv_obj_set_style_text_font(empty, &lm_font_cjk_16, 0);
        lv_obj_center(empty);
    } else {
        for (size_t index = 0; index < scanned_wifi_ssids.size(); ++index) {
            lv_obj_t *item = lv_list_add_btn(list, nullptr, scanned_wifi_ssids[index].c_str());
            lv_obj_set_style_text_font(item, &lm_font_cjk_16, 0);
            lv_obj_set_style_radius(item, 0, 0);
            lv_obj_add_event_cb(item, on_wifi_network_selected, LV_EVENT_CLICKED,
                                (void *)(uintptr_t)index);
        }
    }
    make_wifi_action(overlay, 24, 424, 360, "重新扫描", [](lv_event_t *event) {
        if (lv_event_get_code(event) == LV_EVENT_CLICKED) pending_wifi_setup = true;
    });
    make_wifi_action(overlay, 416, 424, 360, "取消", on_wifi_cancel);
    lvgl_port_unlock();
}

void show_wifi_password_screen() {
    lv_obj_t *overlay = create_wifi_overlay("输入 Wi-Fi 密码");
    lv_obj_t *ssid = lv_label_create(overlay);
    lv_label_set_text(ssid, selected_wifi_ssid.c_str());
    lv_obj_set_width(ssid, 740);
    lv_label_set_long_mode(ssid, LV_LABEL_LONG_DOT);
    lv_obj_set_style_text_font(ssid, &lm_font_cjk_16, 0);
    lv_obj_set_style_text_color(ssid, kMuted, 0);
    lv_obj_set_pos(ssid, 24, 50);

    wifi_password_area = lv_textarea_create(overlay);
    lv_obj_set_size(wifi_password_area, 752, 54);
    lv_obj_set_pos(wifi_password_area, 24, 78);
    lv_obj_set_style_radius(wifi_password_area, 0, 0);
    lv_obj_set_style_text_font(wifi_password_area, &lv_font_montserrat_16, 0);
    lv_textarea_set_one_line(wifi_password_area, true);
    lv_textarea_set_password_mode(wifi_password_area, true);
    lv_textarea_set_placeholder_text(wifi_password_area, "Password (open Wi-Fi may be empty)");
    lv_obj_add_event_cb(wifi_password_area, on_wifi_connect, LV_EVENT_READY, nullptr);

    make_wifi_action(overlay, 24, 140, 360, "连接", on_wifi_connect);
    make_wifi_action(overlay, 416, 140, 360, "返回", on_wifi_back);

    lv_obj_t *keyboard = lv_keyboard_create(overlay);
    lv_obj_set_size(keyboard, 752, 260);
    lv_obj_align(keyboard, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_keyboard_set_textarea(keyboard, wifi_password_area);
    lv_keyboard_set_mode(keyboard, LV_KEYBOARD_MODE_TEXT_LOWER);
}

void start_wifi_setup(const char *notice = "选择要连接的 Wi-Fi") {
    wifi_setup_active = true;
    testing_wifi = false;
    WiFi.disconnect(false);
    WiFi.mode(WIFI_STA);
    set_connection("SCANNING WI-FI", kMuted);
    int count = WiFi.scanNetworks(false, true);
    scanned_wifi_ssids.clear();
    for (int index = 0; index < count; ++index) {
        String candidate = WiFi.SSID(index);
        if (candidate.isEmpty()) continue;
        bool duplicate = false;
        for (const String &known : scanned_wifi_ssids) {
            if (known == candidate) {
                duplicate = true;
                break;
            }
        }
        if (!duplicate) scanned_wifi_ssids.push_back(candidate);
    }
    WiFi.scanDelete();
    show_wifi_list_screen(notice);
}

void begin_wifi() {
    if (wifi_ssid.isEmpty()) {
        pending_wifi_setup = true;
        return;
    }
    WiFi.mode(WIFI_STA);
    WiFi.begin(wifi_ssid.c_str(), wifi_password.c_str());
    last_wifi_attempt = millis();
    if (wifi_connect_started == 0) wifi_connect_started = last_wifi_attempt;
    show_network_message("WI-FI CONNECTING", kMuted, "正在连接 Wi-Fi……");
}
void show_state(JsonDocument &doc) {
    realtime_revision = doc["revision"] | -1;
    const char *title = doc["title"] | "等待识别书籍";
    const char *status = doc["status"] | "waiting";
    JsonArray pages = doc["pages"].as<JsonArray>();
    JsonObject comment = doc["comment"].as<JsonObject>();
    char page[48] = "--";
    if (pages.size() == 2) snprintf(page, sizeof(page), "P%d - P%d", pages[0].as<int>(), pages[1].as<int>());

    current_book_id = String(doc["book_id"] | "");
    String next_comment_id = comment.isNull() ? "" : String(comment["id"] | "");
    if (next_comment_id != current_comment_id) {
        current_comment_id = next_comment_id;
        current_feedback = "";
        feedback_lookup_comment_id = current_comment_id;
    }
    current_comment_page = comment.isNull() ? 0 : comment["page"].as<int>();
    const bool feedback_ready = !current_comment_id.isEmpty();

    lvgl_port_lock(-1);
    lv_label_set_text(connection_label, "LIVE");
    lv_obj_set_style_text_color(connection_label, kGood, 0);
    lv_label_set_text(title_label, title);
    lv_label_set_text(page_label, page);
    if (!comment.isNull() && strlen(comment["text"] | "") > 0) {
        lv_label_set_text(message_label, comment["text"] | "");
        String author = "— ";
        author += String(comment["author"] | "匿名读者");
        if (current_comment_page > 0) author += "  ·  P" + String(current_comment_page);
        lv_label_set_text(author_label, author.c_str());
    } else if (strcmp(status, "stable") == 0) {
        lv_label_set_text(message_label, "本页暂时没有批注");
        lv_label_set_text(author_label, "");
    } else {
        String state = "阅读状态：";
        state += status;
        lv_label_set_text(message_label, state.c_str());
        lv_label_set_text(author_label, "");
    }
    lv_label_set_text(agree_text, current_feedback == "agree" ? "已赞同" : "赞同");
    lv_label_set_text(disagree_text, current_feedback == "disagree" ? "已不赞同" : "不赞同");
    set_button_state(feedback_ready);
    lvgl_port_unlock();
}

void poll_state() {
    HTTPClient http;
    http.setConnectTimeout(3000);
    http.setTimeout(6000);
    String endpoint = web_url + "/api/device/state";
    if (!http.begin(endpoint)) return;
    http.addHeader("Content-Type", "application/json");
    JsonDocument payload;
    payload["machine_code"] = LM_MACHINE_CODE;
    payload["device_token"] = LM_DEVICE_TOKEN;
    String body;
    serializeJson(payload, body);
    int code = http.POST(body);
    if (code != HTTP_CODE_OK) {
        http.end();
        show_network_message("CLOUD OFFLINE", kBad, "云端状态接口暂时不可用");
        return;
    }
    JsonDocument doc;
    DeserializationError error = deserializeJson(doc, http.getStream());
    http.end();
    if (error) {
        show_network_message("DATA ERROR", kBad, "状态数据解析失败");
        return;
    }
    if (doc["vision"].is<JsonObject>()) {
        JsonDocument vision(doc["vision"]);
        show_state(vision);
    } else {
        show_network_message("WAITING", kMuted, "等待识别服务发布阅读状态");
    }
}

void realtime_state_task(void *) {
    for (;;) {
        if (WiFi.status() != WL_CONNECTED || web_url.isEmpty()) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        HTTPClient http;
        http.setConnectTimeout(2000);
        http.setTimeout(12000);
        String endpoint = web_url + "/api/device/state";
        if (!http.begin(endpoint)) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        http.addHeader("Content-Type", "application/json");
        JsonDocument payload;
        payload["machine_code"] = LM_MACHINE_CODE;
        payload["device_token"] = LM_DEVICE_TOKEN;
        payload["revision"] = realtime_revision;
        payload["wait_ms"] = 8000;
        String body;
        serializeJson(payload, body);

        int code = http.POST(body);
        if (code == HTTP_CODE_OK) {
            JsonDocument response;
            if (!deserializeJson(response, http.getStream()) &&
                response["vision"].is<JsonObject>()) {
                StateMessage message{};
                size_t length =
                    serializeJson(response["vision"], message.payload, sizeof(message.payload));
                message.payload[sizeof(message.payload) - 1] = 0;
                if (length > 0 && length < sizeof(message.payload) &&
                    realtime_state_queue != nullptr) {
                    xQueueOverwrite(realtime_state_queue, &message);
                }
            }
        }
        http.end();
        vTaskDelay(pdMS_TO_TICKS(code == HTTP_CODE_OK ? 20 : 1000));
    }
}
void request_pairing_qr() {
    HTTPClient http;
    http.setConnectTimeout(2000);
    http.setTimeout(2500);
    String endpoint = web_url + "/api/device/pairing/start";
    if (!http.begin(endpoint)) return;
    http.addHeader("Content-Type", "application/json");
    JsonDocument payload;
    payload["machine_code"] = LM_MACHINE_CODE;
    payload["device_token"] = LM_DEVICE_TOKEN;
    String body;
    serializeJson(payload, body);
    int code = http.POST(body);
    JsonDocument response;
    bool valid = code == HTTP_CODE_CREATED && !deserializeJson(response, http.getStream());
    http.end();

    lvgl_port_lock(-1);
    if (!valid) {
        lv_label_set_text(connection_label, "PAIRING ERROR");
        lv_obj_set_style_text_color(connection_label, kBad, 0);
        lvgl_port_unlock();
        return;
    }
    String pairing_url = web_url + "/?pair=" + String(response["pairing"]["pairing_token"] | "");
    if (pairing_overlay != nullptr) lv_obj_del(pairing_overlay);
    pairing_overlay = lv_obj_create(lv_scr_act());
    lv_obj_set_size(pairing_overlay, 800, 480);
    lv_obj_center(pairing_overlay);
    lv_obj_set_style_radius(pairing_overlay, 0, 0);
    lv_obj_set_style_border_width(pairing_overlay, 0, 0);
    lv_obj_set_style_bg_color(pairing_overlay, kPaper, 0);
    lv_obj_set_style_bg_opa(pairing_overlay, LV_OPA_COVER, 0);
    lv_obj_clear_flag(pairing_overlay, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(pairing_overlay, close_pairing_overlay, LV_EVENT_CLICKED, nullptr);

    lv_obj_t *heading = lv_label_create(pairing_overlay);
    lv_label_set_text(heading, "扫码绑定页边屏幕");
    lv_obj_set_style_text_font(heading, &lm_font_cjk_16, 0);
    lv_obj_set_style_text_color(heading, kInk, 0);
    lv_obj_align(heading, LV_ALIGN_TOP_MID, 0, 22);

    lv_obj_t *qr = lv_qrcode_create(pairing_overlay, 300, kInk, kPaper);
    lv_qrcode_update(qr, pairing_url.c_str(), pairing_url.length());
    lv_obj_align(qr, LV_ALIGN_CENTER, 0, 4);

    lv_obj_t *hint = lv_label_create(pairing_overlay);
    lv_label_set_text(hint, "请用手机相机扫码 · 5分钟内有效 · 点击空白处关闭");
    lv_obj_set_style_text_font(hint, &lm_font_cjk_16, 0);
    lv_obj_set_style_text_color(hint, kMuted, 0);
    lv_obj_align(hint, LV_ALIGN_BOTTOM_MID, 0, -18);
    lvgl_port_unlock();
}
void fetch_current_feedback(const String &comment_id) {
    if (comment_id.isEmpty()) return;
    HTTPClient http;
    http.setConnectTimeout(3000);
    http.setTimeout(6000);
    String endpoint = web_url + "/api/device/feedback/current";
    if (!http.begin(endpoint)) return;
    http.addHeader("Content-Type", "application/json");
    JsonDocument payload;
    payload["machine_code"] = LM_MACHINE_CODE;
    payload["device_token"] = LM_DEVICE_TOKEN;
    payload["comment_id"] = comment_id;
    String body;
    serializeJson(payload, body);
    int code = http.POST(body);
    if (code == HTTP_CODE_OK) {
        JsonDocument response;
        if (!deserializeJson(response, http.getStream())) {
            const char *action = response["feedback"]["action"] | "";
            if (comment_id == current_comment_id) current_feedback = action;
        }
    }
    http.end();
}
void submit_feedback(const String &action) {
    if (current_comment_id.isEmpty()) return;
    HTTPClient http;
    http.setConnectTimeout(3000);
    http.setTimeout(6000);
    String endpoint = web_url + "/api/device/feedback";
    if (!http.begin(endpoint)) return;
    http.addHeader("Content-Type", "application/json");
    JsonDocument payload;
    payload["machine_code"] = LM_MACHINE_CODE;
    payload["device_token"] = LM_DEVICE_TOKEN;
    payload["comment_id"] = current_comment_id;
    payload["book_id"] = current_book_id;
    payload["page"] = current_comment_page;
    payload["action"] = action;
    String body;
    serializeJson(payload, body);
    int code = http.POST(body);
    http.end();
    lvgl_port_lock(-1);
    if (code == HTTP_CODE_OK || code == HTTP_CODE_CREATED) {
        current_feedback = action;
        lv_label_set_text(connection_label, "FEEDBACK SAVED");
        lv_obj_set_style_text_color(connection_label, kGood, 0);
        lv_label_set_text(agree_text, action == "agree" ? "已赞同" : "赞同");
        lv_label_set_text(disagree_text, action == "disagree" ? "已不赞同" : "不赞同");
    } else {
        lv_label_set_text(connection_label, "FEEDBACK ERROR");
        lv_obj_set_style_text_color(connection_label, kBad, 0);
    }
    set_button_state(true);
    lvgl_port_unlock();
}
}  // namespace

void setup() {
    Serial.begin(115200);
    Board *board = new Board();
    board->init();
#if LVGL_PORT_AVOID_TEARING_MODE
    auto lcd = board->getLCD();
    lcd->configFrameBufferNumber(LVGL_PORT_DISP_BUFFER_NUM);
#if ESP_PANEL_DRIVERS_BUS_ENABLE_RGB && CONFIG_IDF_TARGET_ESP32S3
    auto lcd_bus = lcd->getBus();
    if (lcd_bus->getBasicAttributes().type == ESP_PANEL_BUS_TYPE_RGB) {
        static_cast<BusRGB *>(lcd_bus)->configRGB_BounceBufferSize(lcd->getFrameWidth() * 10);
    }
#endif
#endif
    assert(board->begin());
    assert(lvgl_port_init(board->getLCD(), board->getTouch()));
    lvgl_port_lock(-1);
    create_live_ui();
    lvgl_port_unlock();
    load_network_settings();
    realtime_state_queue = xQueueCreate(1, sizeof(StateMessage));
    if (realtime_state_queue != nullptr) {
        xTaskCreatePinnedToCore(
            realtime_state_task,
            "state-stream",
            12288,
            nullptr,
            1,
            &realtime_state_task_handle,
            0
        );
    }
    begin_wifi();
}

void loop() {
    unsigned long now = millis();
    if (pending_wifi_cancel) {
        pending_wifi_cancel = false;
        wifi_setup_active = false;
        testing_wifi = false;
        begin_wifi();
        return;
    }
    if (pending_wifi_setup) {
        pending_wifi_setup = false;
        start_wifi_setup();
        return;
    }
    if (pending_wifi_connect) {
        pending_wifi_connect = false;
        testing_wifi = true;
        wifi_setup_active = true;
        WiFi.disconnect(false);
        WiFi.mode(WIFI_STA);
        WiFi.begin(selected_wifi_ssid.c_str(), entered_wifi_password.c_str());
        wifi_connect_started = now;
        show_network_message("TESTING WI-FI", kMuted, "正在验证网络密码……");
        return;
    }
    if (testing_wifi) {
        if (WiFi.status() == WL_CONNECTED) {
            wifi_ssid = selected_wifi_ssid;
            wifi_password = entered_wifi_password;
            preferences.begin("living-margins", false);
            preferences.putString("wifi_ssid", wifi_ssid);
            preferences.putString("wifi_pass", wifi_password);
            preferences.end();
            testing_wifi = false;
            wifi_setup_active = false;
            wifi_connect_started = 0;
    if (pending_update_install) {
        pending_update_install = false;
        install_firmware_update();
        return;
    }
    if (pending_update_check) {
        pending_update_check = false;
        check_firmware_update();
        return;
    }            show_network_message("WI-FI SAVED", kGood, "网络已连接并保存");
        } else if (now - wifi_connect_started >= 20000) {
            testing_wifi = false;
            start_wifi_setup("连接失败，请检查密码后重试");
        }
        delay(50);
        return;
    }
    if (wifi_setup_active) {
        delay(20);
        return;
    }
    if (WiFi.status() != WL_CONNECTED) {
        if (wifi_connect_started != 0 && now - wifi_connect_started >= 45000) {
            start_wifi_setup("原网络连接失败，请选择网络");
        } else if (now - last_wifi_attempt >= 15000) {
            begin_wifi();
        }
        delay(50);
        return;
    }
    wifi_connect_started = 0;
    if (realtime_state_queue != nullptr &&
        xQueueReceive(realtime_state_queue, &incoming_state_message, 0) == pdTRUE) {
        JsonDocument realtime_doc;
        if (!deserializeJson(realtime_doc, incoming_state_message.payload)) {
            show_state(realtime_doc);
            last_realtime_update = now;
        }
    }
    if (pending_pairing_request) {
        pending_pairing_request = false;
        request_pairing_qr();
    }
    if (!feedback_lookup_comment_id.isEmpty()) {
        String comment_id = feedback_lookup_comment_id;
        feedback_lookup_comment_id = "";
        fetch_current_feedback(comment_id);
    }
    if (!pending_feedback.isEmpty()) {
        String action = pending_feedback;
        pending_feedback = "";
        submit_feedback(action);
    }
    if ((last_realtime_update == 0 || now - last_realtime_update >= 12000) &&
        now - last_poll >= 3000) {
        last_poll = now;
        poll_state();
    }
    delay(20);
}
