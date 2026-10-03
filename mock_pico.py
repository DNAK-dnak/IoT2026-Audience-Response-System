import asyncio
import json
from bless import (
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions
)

# Cấu hình UUID (Chuyển về chuẩn 128-bit để laptop hiểu được)
SERVICE_UUID = "00001234-0000-1000-8000-00805f9b34fb"
TX_CHAR_UUID = "00005678-0000-1000-8000-00805f9b34fb" # Gửi lên Server
RX_CHAR_UUID = "00009abc-0000-1000-8000-00805f9b34fb" # Nhận từ Server

# Thông số giả lập
DEV_ID = "8" # Giả lập đây là thiết bị số 8
DEVICE_NAME = f"PicoRemote_{DEV_ID}"

# Biến trạng thái mô phỏng phần cứng
current_mode = 2
mssv_buffer = ""
seq = 0
is_quiz_locked = False
my_server = None

# Hàm xử lý khi Server chủ động gửi lệnh xuống (GATT Write)
def write_callback(characteristic: BlessGATTCharacteristic, value: bytes, **kwargs):
    global current_mode, mssv_buffer, is_quiz_locked
    
    # Giải mã lệnh JSON từ Server
    cmd_string = value.decode('utf-8')
    try:
        data = json.loads(cmd_string)
        cmd = data.get("cmd")
        
        if cmd == "MODE_1":
            current_mode = 1
            print(f"\n[!] SERVER RA LỆNH: Chuyển về CHẾ ĐỘ 1 (Chờ)")
        elif cmd == "MODE_2":
            current_mode = 2
            mssv_buffer = ""
            print(f"\n[!] SERVER RA LỆNH: Chuyển về CHẾ ĐỘ 2 (Nhập MSSV)")
        elif cmd == "MODE_3":
            current_mode = 3
            is_quiz_locked = False
            print(f"\n[!] SERVER RA LỆNH: Chuyển về CHẾ ĐỘ 3 (Trắc nghiệm)")
        elif cmd == "UNLOCK":
            is_quiz_locked = False
            print(f"\n[!] SERVER RA LỆNH: Đã mở khóa phím.")
            
        characteristic.value = value
    except Exception as e:
        print(f"Lỗi đọc lệnh từ Server: {e}")

# Hàm đóng gói và gửi JSON y hệt hàm send_button_data của Pico W
def send_notify(payload_dict):
    global my_server
    if my_server:
        json_string = json.dumps(payload_dict)
        # Cập nhật giá trị vào hộp thư TX và phát Notify đi
        my_server.get_characteristic(TX_CHAR_UUID).value = json_string.encode('utf-8')
        my_server.update_value(SERVICE_UUID, TX_CHAR_UUID)
        print(f" [BLE TX] Đã gửi: {json_string}")

# Vòng lặp mô phỏng người dùng gõ phím vật lý
async def keyboard_simulation_loop():
    global current_mode, mssv_buffer, seq, is_quiz_locked
    
    print("\n--- BẢNG ĐIỀU KHIỂN PICO ẢO ---")
    print("Gõ phím số (0-9), 'ENTER' hoặc 'BACK' để giả lập bấm nút.")
    
    while True:
        # Chờ người dùng nhập từ bàn phím terminal
        user_input = await asyncio.to_thread(input, "> Nhập phím: ")
        user_input = user_input.strip().upper()
        
        if not user_input:
            continue
            
        seq += 1
        
        # XỬ LÝ CHẾ ĐỘ 2: NHẬP MSSV
        if current_mode == 2:
            if user_input == "ENTER":
                if mssv_buffer:
                    send_notify({"dev": int(DEV_ID), "mssv": mssv_buffer, "hw": "laptop_mock", "seq": seq})
                    mssv_buffer = ""
            elif user_input == "BACK":
                if mssv_buffer:
                    mssv_buffer = mssv_buffer[:-1]
                    send_notify({"dev": int(DEV_ID), "key": "BACKSPACE"})
            else:
                mssv_buffer += user_input
                send_notify({"dev": int(DEV_ID), "key": user_input})
                
        # XỬ LÝ CHẾ ĐỘ 3: TRẮC NGHIỆM
        elif current_mode == 3:
            if is_quiz_locked:
                print(" Phím đang bị khóa! Chờ Server mở khóa.")
                continue
                
            if user_input in ["1", "2", "3", "4"]:
                # Chốt thẳng đáp án (bỏ qua mô phỏng radio cho nhanh)
                send_notify({"dev": int(DEV_ID), "q": 1, "choice": int(user_input), "seq": seq})
                is_quiz_locked = True
            else:
                print(" Chỉ được nhập 1, 2, 3, 4 trong chế độ này.")

async def main():
    global my_server
    print(f"Khởi tạo phần cứng ảo: {DEVICE_NAME}...")
    
    # Khởi tạo Server
    my_server = BlessServer(name=DEVICE_NAME)
    my_server.write_request_func = write_callback
    
    # Đăng ký Service & Characteristic
    await my_server.add_new_service(SERVICE_UUID)
    await my_server.add_new_characteristic(
        SERVICE_UUID, TX_CHAR_UUID,
        GATTCharacteristicProperties.notify,
        GATTAttributePermissions.readable,
        bytearray(b"")
    )
    await my_server.add_new_characteristic(
        SERVICE_UUID, RX_CHAR_UUID,
        GATTCharacteristicProperties.write,
        GATTAttributePermissions.writeable,
        bytearray(b"")
    )
    
    # Bắt đầu phát sóng BLE
    await my_server.start()
    print(f" Đang phát sóng BLE: {DEVICE_NAME}")
    
    # Chạy vòng lặp giả lập bàn phím
    await keyboard_simulation_loop()

if __name__ == "__main__":
    asyncio.run(main())