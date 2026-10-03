import asyncio
import json
from bleak import BleakScanner, BleakClient

DEVICE_NAME = "PicoRemote"
RX_CHAR_UUID = "00009abc-0000-1000-8000-00805f9b34fb" # Hộp thư nhận lệnh của Pico W
TX_CHAR_UUID = "00005678-0000-1000-8000-00805f9b34fb" # Hộp thư nhận dữ liệu từ Pico W

# Giả lập Database và Trạng thái lớp học
GLOBAL_CLASS_MODE = "MODE_3" # Thử đổi thành "MODE_2" để test
mock_database = {
    # Giả lập Remote số 7 (của cậu) đã báo danh thành công
    "7": {"mssv": "23134024", "verified": True} 
}

# 1. Hàm gửi lệnh từ Laptop -> Pico W
async def send_command_to_device(client: BleakClient, cmd_string: str):
    payload = json.dumps({"cmd": cmd_string})
    await client.write_gatt_char(RX_CHAR_UUID, payload.encode('utf-8'))
    print(f"[SERVER TX] Đã lệnh cho {client.address} chuyển sang: {cmd_string}")

# 2. Hàm xử lý dữ liệu từ Pico W -> Laptop
def notification_handler(sender, data):
    try:
        json_string = data.decode('utf-8')
        payload = json.loads(json_string)
        
        dev_id = payload.get("dev", "Unknown")
        
        if "mssv" in payload:
            mssv = payload["mssv"]
            # Khi sinh viên nộp MSSV thành công, cập nhật mock_database tại đây
            mock_database[str(dev_id)] = {"mssv": mssv, "verified": True}
            print(f"[BÁO DANH] Remote {dev_id} | MSSV: {mssv}")
            
        elif "choice" in payload:
            answers_map = {1: 'A', 2: 'B', 3: 'C', 4: 'D'}
            answer_letter = answers_map.get(payload["choice"], "Lỗi phím")
            print(f"[TRẮC NGHIỆM] Remote {dev_id} | Câu {payload.get('q', 1)}: Chọn {answer_letter}")
        
        elif "key" in payload:
            print(f"[GÕ PHÍM] Remote {dev_id} gõ: {payload['key']}")
            
    except json.JSONDecodeError:
        pass

# 3. Hàm cảnh báo khi sập nguồn/mất sóng
def handle_disconnect(client: BleakClient):
    print(f"\n[CẢNH BÁO] Thiết bị {client.address} ĐÃ MẤT KẾT NỐI!")

# 4. Luồng xử lý khi có mạch Pico W mới cắm điện
async def handle_new_connection(device):
    async with BleakClient(device, disconnected_callback=handle_disconnect) as client:
        print(f" Đã kết nối thành công với: {device.address}")
        
        # Giả lập trích xuất dev_id (Thực tế sẽ lấy từ gói tin hello hoặc MAC)
        dev_id = "7" 
        
        # === LOGIC ĐIỀU HƯỚNG MÀ CẬU VỪA ĐỀ XUẤT ===
        if GLOBAL_CLASS_MODE == "MODE_3":
            if dev_id in mock_database and mock_database[dev_id].get("verified"):
                print(f" -> Remote {dev_id} đã có phép. Cho phép tiếp tục bài quiz.")
                await send_command_to_device(client, "MODE_3")
            else:
                print(f" -> Remote {dev_id} chưa báo danh! Ép quay về nhập MSSV.")
                await send_command_to_device(client, "MODE_2")
                
        elif GLOBAL_CLASS_MODE == "MODE_2":
            print(f" -> Lớp đang báo danh. Yêu cầu Remote {dev_id} nhập MSSV.")
            await send_command_to_device(client, "MODE_2")
            
        # Mở hòm thư để lắng nghe sinh viên bấm nút
        await client.start_notify(TX_CHAR_UUID, notification_handler)
        
        while True:
            await asyncio.sleep(1)

# 5. Hàm chính
async def main():
    print(f" Đang rà quét tìm '{DEVICE_NAME}'...")
    device = await BleakScanner.find_device_by_name(DEVICE_NAME)

    if device:
        print(f" Đã khóa mục tiêu: {device.name} [{device.address}]")
        await handle_new_connection(device)
    else:
        print(f" Không tìm thấy thiết bị nào.")

if __name__ == "__main__":
    asyncio.run(main())
