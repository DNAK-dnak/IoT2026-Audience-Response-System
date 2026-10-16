from machine import Pin
import time
import json
from lib_ledbutton import LEDButton
from lib_ble import BLERemote

# 1. CÁC BIẾN QUẢN LÝ TRẠNG THÁI
MODE_FREE = 1
MODE_NUMPAD = 2
MODE_QUIZ = 3

current_mode = MODE_NUMPAD  # Thiết bị luôn bắt đầu ở chế độ tự do
mssv_buffer = ""
is_quiz_locked = False
current_quiz_choice = None

# 2. KHAI BÁO PHẦN CỨNG
led_pw = Pin(5, Pin.OUT, value=1)
led_conn = Pin(4, Pin.OUT, value=0)

leds = {
    6: Pin(0, Pin.OUT, value=0),
    7: Pin(1, Pin.OUT, value=0),
    8: Pin(2, Pin.OUT, value=0),
    9: Pin(3, Pin.OUT, value=0)
}

def blink_if_has_led(pin_num, duration=0.1):
    if pin_num in leds:
        leds[pin_num].value(1); time.sleep(duration); leds[pin_num].value(0)

# 3. HÀM XỬ LÝ LỆNH TỪ SERVER (BLE RX)
def process_server_command(msg):
    global current_mode, mssv_buffer, is_quiz_locked, current_quiz_choice
    try:
        data = json.loads(msg)
        cmd = data.get("cmd")
        
        if cmd == "MODE_1":
            current_mode = MODE_FREE
            print(">> [SERVER] Kích hoạt CHẾ ĐỘ 1 (Tự do)")
            for p in leds.values(): p.value(0)
            
        elif cmd == "MODE_2":
            current_mode = MODE_NUMPAD
            mssv_buffer = "" # Xóa sạch bộ đệm MSSV cũ
            print(">> [SERVER] Kích hoạt CHẾ ĐỘ 2 (Nhập MSSV)")
            for p in leds.values(): p.value(0)
            
        elif cmd == "MODE_3":
            current_mode = MODE_QUIZ
            is_quiz_locked = False
            current_quiz_choice = None
            print(">> [SERVER] Kích hoạt CHẾ ĐỘ 3 (Trắc nghiệm)")
            for p in leds.values(): p.value(0)
            
        elif cmd == "UNLOCK":
            is_quiz_locked = False
            current_quiz_choice = None
            print(">> [SERVER] Đã mở khóa phím cho câu hỏi tiếp theo!")
            for p in leds.values(): p.value(0)
        elif cmd == "INVALID_MSSV":
            current_mode = MODE_NUMPAD
            mssv_buffer = "" # Ép xóa sạch bộ đệm để gõ lại từ đầu
            print(">> [SERVER] Lỗi: MSSV không hợp lệ. Đã reset bộ đệm.")
            
            # Hiệu ứng đèn báo lỗi (Nháy cả 4 đèn 3 lần nhanh)
            for _ in range(3):
                for p in leds.values(): p.value(1)
                time.sleep(0.1)
                for p in leds.values(): p.value(0)
                time.sleep(0.1)
            
    except ValueError:
        print(f"Lỗi cú pháp lệnh JSON từ Server: {msg}")

# Truyền hàm xử lý lệnh vào thư viện BLE
ble_remote = BLERemote(name="PicoRemote_7", dev_id=7, conn_led_pin=led_conn, cmd_callback=process_server_command)

# 4. HÀM NÃO BỘ ĐIỀU KHIỂN (BLE TX)
def main_controller_logic(pressed_pin, button_value):
    global current_mode, mssv_buffer, is_quiz_locked, current_quiz_choice
    
    if current_mode == MODE_FREE:
        blink_if_has_led(pressed_pin)
        
    elif current_mode == MODE_NUMPAD:
        if button_value == 'ENTER':
            if len(mssv_buffer) > 0:
                ble_remote.send_mssv_data(mssv_buffer)
                print(f"-> Gửi chốt MSSV: {mssv_buffer}")
                mssv_buffer = ""
                for led in leds.values(): led.value(1)
                time.sleep(0.3)
                for led in leds.values(): led.value(0)
        elif button_value == 'BACKSPACE':
            # Chỉ thực hiện xóa nếu bộ đệm đang có dữ liệu
            if len(mssv_buffer) > 0:
                mssv_buffer = mssv_buffer[:-1]
                # Bắn tín hiệu "BACKSPACE" lên cho Server biết để cập nhật giao diện
                ble_remote.send_keypress("BACKSPACE") 
                print(f"[MODE 2] Xóa. MSSV hiện tại: {mssv_buffer}")
            blink_if_has_led(pressed_pin)
        else: # Các phím số 0-9
            if len(mssv_buffer) < 8:
                mssv_buffer += button_value
                ble_remote.send_keypress(button_value) # Bắn phím vừa gõ lên Server
                print(f"[MODE 2] Nhập: {button_value} -> MSSV: {mssv_buffer}")
                blink_if_has_led(pressed_pin)

    elif current_mode == MODE_QUIZ:
        if is_quiz_locked:
            return 
            
        if pressed_pin in leds:
            current_quiz_choice = button_value
            for p in leds.values(): p.value(0)
            leds[pressed_pin].value(1)
            
        elif button_value == 'ENTER':
            if current_quiz_choice is not None:
                ble_remote.send_quiz_data(current_quiz_choice, question_id=1)
                is_quiz_locked = True
                for p in leds.values(): p.value(1)
                time.sleep(0.3)
                for p in leds.values(): p.value(0)

# 5. KHỞI TẠO NÚT NHẤN
btn_1 = LEDButton(6, '1', main_controller_logic)
btn_2 = LEDButton(7, '2', main_controller_logic)
btn_3 = LEDButton(8, '3', main_controller_logic)
btn_4 = LEDButton(9, '4', main_controller_logic)
btn_5 = LEDButton(10, '5', main_controller_logic)
btn_6 = LEDButton(11, '6', main_controller_logic)
btn_7 = LEDButton(12, '7', main_controller_logic)
btn_8 = LEDButton(13, '8', main_controller_logic)
btn_9 = LEDButton(14, '9', main_controller_logic)
btn_0 = LEDButton(15, '0', main_controller_logic)
btn_back  = LEDButton(16, 'BACKSPACE', main_controller_logic)
btn_enter = LEDButton(17, 'ENTER', main_controller_logic)

while True:
    time.sleep(0.1)

