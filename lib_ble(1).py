import bluetooth
import json

class BLERemote:
    # Bổ sung tham số cmd_callback vào hàm khởi tạo
    def __init__(self, name="PicoRemote", dev_id=7, conn_led_pin=None, cmd_callback=None):
        self.name = name
        self.dev_id = dev_id
        self.conn_led = conn_led_pin
        self.cmd_callback = cmd_callback # Lưu hàm xử lý lệnh từ main.py
        self.seq = 0
        
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        
        # Cấu hình UUID cho TX (Gửi đi) và RX (Nhận về)
        UART_UUID = bluetooth.UUID(0x1234)
        TX_CHAR_UUID = (bluetooth.UUID(0x5678), bluetooth.FLAG_NOTIFY)
        RX_CHAR_UUID = (bluetooth.UUID(0x9ABC), bluetooth.FLAG_WRITE) # Hộp thư nhận lệnh
        
        SERVICES = ((UART_UUID, (TX_CHAR_UUID, RX_CHAR_UUID,)),)
        
        # Đăng ký Service và lấy 2 tay cầm (handle) tương ứng
        ((self.tx_char_handle, self.rx_char_handle,),) = self.ble.gatts_register_services(SERVICES)
        self.conn_handle = None
        
        self.ble.irq(self.ble_irq)
        self.start_advertising()

    def ble_irq(self, event, data):
        if event == 1: # _IRQ_CENTRAL_CONNECT
            self.conn_handle, _, _ = data
            print(f"[{self.name}] Đã kết nối với Web Server!")
            if self.conn_led:
                self.conn_led.value(1)
                
        elif event == 2: # _IRQ_CENTRAL_DISCONNECT
            self.conn_handle = None
            print(f"[{self.name}] Mất kết nối! Đang phát sóng lại...")
            if self.conn_led:
                self.conn_led.value(0)
            self.start_advertising()
            
        elif event == 3: # _IRQ_GATTS_WRITE (Sự kiện khi Laptop gửi lệnh xuống)
            conn_handle, value_handle = data
            # Kiểm tra đúng ID kết nối và đúng hộp thư RX
            if conn_handle == self.conn_handle and value_handle == self.rx_char_handle:
                rx_bytes = self.ble.gatts_read(self.rx_char_handle)
                # Gửi chuỗi byte thô về hàm callback ở main.py để xử lý
                if self.cmd_callback:
                    self.cmd_callback(rx_bytes)

    def start_advertising(self):
        payload = bytearray([len(self.name) + 1, 0x09]) + self.name.encode('utf-8')
        self.ble.gap_advertise(100000, payload)

    def send_quiz_data(self, button_value, question_id=1):
        if self.conn_handle is not None:
            self.seq += 1
            payload_dict = {
                "dev": self.dev_id,
                "q": question_id,
                "choice": int(button_value),
                "seq": self.seq
            }
            json_string = json.dumps(payload_dict)
            self.ble.gatts_notify(self.conn_handle, self.tx_char_handle, json_string.encode('utf-8'))
            print(f"[BLE TX] Quiz: {json_string}")

    def send_mssv_data(self, mssv_string):
        if self.conn_handle is not None:
            self.seq += 1
            payload_dict = {
                "dev": self.dev_id,
                "mssv": mssv_string,
                "hw": "pico2w",
                "seq": self.seq
            }
            json_string = json.dumps(payload_dict)
            self.ble.gatts_notify(self.conn_handle, self.tx_char_handle, json_string.encode('utf-8'))
            print(f"[BLE TX] Báo danh: {json_string}")
            
    def send_keypress(self, key_value):
        if self.conn_handle is not None:
            payload_dict = {"dev": self.dev_id, "key": key_value}
            json_string = json.dumps(payload_dict)
            self.ble.gatts_notify(self.conn_handle, self.tx_char_handle, json_string.encode('utf-8'))
