`timescale 1ns / 1ps
// ================================================================
//  lift_fpga_top.v  -  Top-level with UART telemetry (4-byte frame)
//
//  UART packet format (sent at ~10 Hz, 4 bytes per frame):
//
//  Byte 0  [STATUS]  - bit7 always 1 (frame start marker)
//    [7]   = 1              start-of-frame
//    [6]   = emergency_latched
//    [5]   = door_w
//    [4]   = Up_w
//    [3]   = Down_w
//    [2:0] = current_floor_w
//
//  Byte 1  [CABIN REQ]  - bit7 always 0
//    [7]   = 0
//    [6:5] = 00
//    [4:0] = sw[4:0]        cabin requests floors 0-4
//
//  Byte 2  [HALL UP]  - bit7 always 0
//    [7]   = 0
//    [5:4] = 00
//    [4:1] = sw[8:5]        hall UP requests floors 0-3
//    [0]   = idle_w
//
//  Byte 3  [HALL DOWN]  - bit7 always 0
//    [7]   = 0
//    [6:5] = 00
//    [4:1] = sw[12:9]       hall DOWN requests floors 1-4
//    [0]   = 0  spare
//
//  Python syncs on byte where bit7==1, reads next 3 bytes.
// ================================================================

module lift_fpga_top(
    input        clk,
    input        btnC,
    input        btnU,
    input [12:0] sw,

    output [2:0] led,
    output       led_idle,
    output       led_door,

    output reg [6:0] seg,
    output reg [3:0] an,

    output       uart_tx       // Add to XDC: PACKAGE_PIN A18
);

// ── Clock divider ────────────────────────────────────────────────
reg [26:0] clk_div;
wire slow_clk = clk_div[26];
wire disp_clk = clk_div[17];
always @(posedge clk) clk_div <= clk_div + 1;

// ── Emergency latch ──────────────────────────────────────────────
reg emergency_latched;
always @(posedge clk) begin
    if (btnC)      emergency_latched <= 1'b0;
    else if (btnU) emergency_latched <= 1'b1;
end

// ── Wire mapping ─────────────────────────────────────────────────
wire [7:0] cabin_req_mapped  = {3'b000,  sw[4:0]};
wire [7:0] hall_up_mapped    = {4'b0000, sw[8:5]};
wire [7:0] hall_down_mapped  = {3'b000,  sw[12:9], 1'b0};

wire [2:0] current_floor_w;
wire       Up_w, Down_w, idle_w, door_w;

Lift8 uut (
    .clk          (slow_clk),
    .reset        (btnC),
    .cabin_req    (cabin_req_mapped),
    .hall_up      (hall_up_mapped),
    .hall_down    (hall_down_mapped),
    .idle         (idle_w),
    .door         (door_w),
    .Up           (Up_w),
    .Down         (Down_w),
    .current_floor(current_floor_w),
    .emergency_stop(emergency_latched)
);

assign led      = current_floor_w;
assign led_idle = idle_w;
assign led_door = door_w;

// ── 7-Segment display (unchanged) ───────────────────────────────
reg [6:0] floor_seg;
wire [6:0] char_O      = 7'b1000000;
wire [6:0] char_P      = 7'b0001100;
wire [6:0] char_E      = 7'b0000110;
wire [6:0] char_N      = 7'b1001000;
wire [6:0] char_S_full = 7'b0010010;
wire [6:0] char_T_full = 7'b0000111;
wire [6:0] char_R_full = 7'b0101111;

always @(*) begin
    case(current_floor_w)
        3'b000: floor_seg = 7'b1000000;
        3'b001: floor_seg = 7'b1111001;
        3'b010: floor_seg = 7'b0100100;
        3'b011: floor_seg = 7'b0110000;
        3'b100: floor_seg = 7'b0011001;
        default: floor_seg = 7'b0111111;
    endcase
end

reg [6:0] dir_seg;
always @(*) begin
    if      (Up_w && !Down_w)  dir_seg = 7'b1000001;
    else if (Down_w && !Up_w)  dir_seg = 7'b0100001;
    else                       dir_seg = 7'b0111111;
end

reg [1:0] mux_state;
reg [1:0] disp_mode;
reg [9:0] rst_cnt;

always @(posedge disp_clk or posedge btnC) begin
    if (btnC) begin
        disp_mode <= 3;
        rst_cnt   <= 0;
    end else begin
        mux_state <= mux_state + 1;
        case (disp_mode)
            0: begin
                if (emergency_latched) disp_mode <= 1;
                if (led_door) begin
                    case (mux_state)
                        2'b00: begin an <= 4'b0111; seg <= char_O; end
                        2'b01: begin an <= 4'b1011; seg <= char_P; end
                        2'b10: begin an <= 4'b1101; seg <= char_E; end
                        2'b11: begin an <= 4'b1110; seg <= char_N; end
                    endcase
                end else begin
                    if (an == 4'b1110) begin an <= 4'b0111; seg <= dir_seg;   end
                    else               begin an <= 4'b1110; seg <= floor_seg; end
                end
            end
            1: begin
                case (mux_state)
                    2'b00: begin an <= 4'b0111; seg <= char_S_full; end
                    2'b01: begin an <= 4'b1011; seg <= char_T_full; end
                    2'b10: begin an <= 4'b1101; seg <= char_O;      end
                    2'b11: begin an <= 4'b1110; seg <= char_P;      end
                endcase
                if (led_door) disp_mode <= 2;
            end
            2: begin
                if (clk_div[25]) begin
                    an <= 4'b1110; seg <= floor_seg;
                end else begin
                    case (mux_state)
                        2'b00: begin an <= 4'b0111; seg <= char_O; end
                        2'b01: begin an <= 4'b1011; seg <= char_P; end
                        2'b10: begin an <= 4'b1101; seg <= char_E; end
                        2'b11: begin an <= 4'b1110; seg <= char_N; end
                    endcase
                end
            end
            3: begin
                rst_cnt <= rst_cnt + 1;
                case (mux_state)
                    2'b00: begin an <= 4'b0111; seg <= char_R_full; end
                    2'b01: begin an <= 4'b1011; seg <= char_S_full; end
                    2'b10: begin an <= 4'b1101; seg <= char_T_full; end
                    2'b11: begin an <= 4'b1111; end
                endcase
                if (rst_cnt > 800) disp_mode <= 0;
            end
        endcase
    end
end

// ════════════════════════════════════════════════════════════════
//  UART TELEMETRY  -  4-byte frame at exactly 10 Hz
// ════════════════════════════════════════════════════════════════

reg [23:0] uart_tick_cnt;
reg        uart_tick;

always @(posedge clk or posedge btnC) begin
    if (btnC) begin
        uart_tick_cnt <= 0;
        uart_tick     <= 0;
    end else begin
        uart_tick <= 1'b0;
        if (uart_tick_cnt == 24'd9_999_999) begin
            uart_tick_cnt <= 0;
            uart_tick     <= 1'b1;
        end else begin
            uart_tick_cnt <= uart_tick_cnt + 1;
        end
    end
end

// ── 4 bytes ───────────────────────────────────────────────────────
wire [7:0] byte0 = {1'b1,
                    emergency_latched,
                    door_w,
                    Up_w,
                    Down_w,
                    current_floor_w};

wire [7:0] byte1 = {1'b0, 2'b00, sw[4:0]};

wire [7:0] byte2 = {1'b0, 2'b00, sw[8:5], idle_w};

wire [7:0] byte3 = {1'b0, 2'b00, sw[12:9], 1'b0};

// ── Sequencer ────────────────────────────────────────────────────
reg [2:0] seq_state;
reg [7:0] tx_data_reg;
reg       tx_start_reg;
wire      tx_busy_w;

localparam SEQ_IDLE = 3'd0;
localparam SEQ_B0   = 3'd1;
localparam SEQ_B1   = 3'd2;
localparam SEQ_B2   = 3'd3;
localparam SEQ_B3   = 3'd4;

always @(posedge clk or posedge btnC) begin
    if (btnC) begin
        seq_state    <= SEQ_IDLE;
        tx_data_reg  <= 8'h00;
        tx_start_reg <= 1'b0;
    end else begin
        tx_start_reg <= 1'b0;
        case (seq_state)
            SEQ_IDLE: if (uart_tick)                    begin tx_data_reg <= byte0; tx_start_reg <= 1'b1; seq_state <= SEQ_B0; end
            SEQ_B0:   if (!tx_busy_w && !tx_start_reg) begin tx_data_reg <= byte1; tx_start_reg <= 1'b1; seq_state <= SEQ_B1; end
            SEQ_B1:   if (!tx_busy_w && !tx_start_reg) begin tx_data_reg <= byte2; tx_start_reg <= 1'b1; seq_state <= SEQ_B2; end
            SEQ_B2:   if (!tx_busy_w && !tx_start_reg) begin tx_data_reg <= byte3; tx_start_reg <= 1'b1; seq_state <= SEQ_B3; end
            SEQ_B3:   if (!tx_busy_w && !tx_start_reg) seq_state <= SEQ_IDLE;
        endcase
    end
end

uart_tx UART(
    .clk     (clk),
    .rst     (btnC),
    .tx_data (tx_data_reg),
    .tx_start(tx_start_reg),
    .tx      (uart_tx),
    .tx_busy (tx_busy_w)
);

endmodule