import asyncio
import json
from bless import (
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions
)

# Cấu hình UUID 
SERVICE_UUID = "00001234-0000-1000-8000-00805f9b34fb"
TX_CHAR_UUID = "00005678-0000-1000-8000-00805f9b34fb"
RX_CHAR_UUID = "00009abc-0000-1000-8000-00805f9b34fb"

# Thông số giả lập
DEV_ID = "8" 
DEVICE_NAME = f"PicoRemote_{DEV_ID}"

# Biến trạng thái
current_mode = 2
mssv_buffer = ""
seq = 0
is_quiz_locked = False
current_quiz_choice = None
my_server = None

# Cờ theo dõi trạng thái kết nối
has_connected = False 

# Hàm xử lý khi Server chủ động gửi lệnh xuống (BLE RX)
def write_callback(characteristic: BlessGATTCharacteristic, value: bytes, **kwargs):
    global current_mode, mssv_buffer, is_quiz_locked, current_quiz_choice, has_connected
    
    # Lần đầu tiên nhận được tín hiệu từ Server -> Chắc chắn đã kết nối
    if not has_connected:
        has_connected = True
        print("\n\n[+] THÀNH CÔNG: ĐÃ KẾT NỐI VỚI MÁY CHỦ (SERVER)!")
        print("[-] Đang đồng bộ trạng thái lớp học...\n")
    
    cmd_string = value.decode('utf-8')
    try:
        data = json.loads(cmd_string)
        cmd = data.get("cmd")
        
        if cmd == "MODE_1":
            current_mode = 1
            print(">> [SERVER] Kích hoạt CHẾ ĐỘ 1 (Tự do)")
            
        elif cmd == "MODE_2":
            current_mode = 2
            mssv_buffer = ""
            print(">> [SERVER] Kích hoạt CHẾ ĐỘ 2 (Nhập MSSV)")
            
        elif cmd == "MODE_3":
            current_mode = 3
            is_quiz_locked = False
            current_quiz_choice = None
            print(">> [SERVER] Kích hoạt CHẾ ĐỘ 3 (Trắc nghiệm)")
            
        elif cmd == "UNLOCK":
            is_quiz_locked = False
            current_quiz_choice = None
            print(">> [SERVER] Đã mở khóa phím cho câu hỏi tiếp theo!")
            
        elif cmd == "INVALID_MSSV":
            current_mode = 2
            mssv_buffer = ""
            print(">> [SERVER] Lỗi: MSSV không hợp lệ. Đã reset bộ đệm.")
            
        characteristic.value = value
    except Exception as e:
        print(f"Lỗi đọc lệnh từ Server: {e}")

# Hàm đóng gói và gửi JSON 
def send_notify(payload_dict):
    global my_server
    if my_server:
        json_string = json.dumps(payload_dict)
        my_server.get_characteristic(TX_CHAR_UUID).value = json_string.encode('utf-8')
        my_server.update_value(SERVICE_UUID, TX_CHAR_UUID)
        
        if "mssv" in payload_dict:
            print(f"[BLE TX] Báo danh: {json_string}")
        elif "choice" in payload_dict:
            print(f"[BLE TX] Quiz: {json_string}")

# Vòng lặp mô phỏng NÃO BỘ ĐIỀU KHIỂN (BLE TX)
async def keyboard_simulation_loop():
    global current_mode, mssv_buffer, seq, is_quiz_locked, current_quiz_choice
    
    print("\n--- BẢNG ĐIỀU KHIỂN PICO ẢO ---")
    print("Gõ phím số (0-9), 'ENTER' hoặc 'BACKSPACE' (gõ tắt 'BACK') để giả lập bấm nút.")
    
    while True:
        user_input = await asyncio.to_thread(input, "> ")
        user_input = user_input.strip().upper()
        
        if user_input == "BACK":
            user_input = "BACKSPACE"
            
        if not user_input:
            continue
            
        if current_mode == 1:
            print(f"[MODE 1] Nút {user_input} hoạt động độc lập")
            
        elif current_mode == 2:
            if user_input == "ENTER":
                if len(mssv_buffer) > 0:
                    seq += 1
                    send_notify({"dev": int(DEV_ID), "mssv": mssv_buffer, "hw": "laptop_mock", "seq": seq})
                    print(f"-> Gửi chốt MSSV: {mssv_buffer}")
                    mssv_buffer = ""
            elif user_input == "BACKSPACE":
                if len(mssv_buffer) > 0:
                    mssv_buffer = mssv_buffer[:-1]
                    send_notify({"dev": int(DEV_ID), "key": "BACKSPACE"})
                    print(f"[MODE 2] Xóa. MSSV hiện tại: {mssv_buffer}")
            else:
                if len(mssv_buffer) < 8:
                    mssv_buffer += user_input
                    send_notify({"dev": int(DEV_ID), "key": user_input})
                    print(f"[MODE 2] Nhập: {user_input} -> MSSV: {mssv_buffer}")
                    
        elif current_mode == 3:
            if is_quiz_locked:
                print("[MODE 3] Đã khóa phím! Chờ giáo viên chuyển câu.")
                continue
                
            if user_input in ["1", "2", "3", "4"]:
                current_quiz_choice = user_input
                print(f"[MODE 3] Đang chọn đáp án: {current_quiz_choice}")
                
            elif user_input == "ENTER":
                if current_quiz_choice is not None:
                    seq += 1
                    send_notify({"dev": int(DEV_ID), "q": 1, "choice": int(current_quiz_choice), "seq": seq})
                    is_quiz_locked = True
                    print("[MODE 3] Đã chốt đáp án và khóa bàn phím.")

async def main():
    global my_server
    print(f"Khởi tạo phần cứng ảo: {DEVICE_NAME}...")
    
    my_server = BlessServer(name=DEVICE_NAME)
    my_server.write_request_func = write_callback
    
    await my_server.add_new_service(SERVICE_UUID)
    
    await my_server.add_new_characteristic(
        SERVICE_UUID, TX_CHAR_UUID,
        GATTCharacteristicProperties.notify,
        bytearray(b""), GATTAttributePermissions.readable
    )
    
    await my_server.add_new_characteristic(
        SERVICE_UUID, RX_CHAR_UUID,
        GATTCharacteristicProperties.write,
        bytearray(b""), GATTAttributePermissions.writeable
    )
    
    await my_server.start()
    print(f" Đang phát sóng BLE: {DEVICE_NAME}")
    
    await keyboard_simulation_loop()

if __name__ == "__main__":
    asyncio.run(main())
