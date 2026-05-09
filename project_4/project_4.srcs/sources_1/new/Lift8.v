`timescale 1ns / 1ps

module Lift8(
    input clk,
    input reset,
    input [7:0] cabin_req,
    input [7:0] hall_up,
    input [7:0] hall_down,
    input emergency_stop,
    output reg idle,
    output reg door,
    output reg Up,
    output reg Down,
    output reg [2:0] current_floor,
    output reg [2:0] max_request,
    output reg [2:0] min_request
);

    // internal registers
    reg [7:0] cabin_requests, up_requests, down_requests, all_requests;
    reg door_timer;

    // wires
    wire [7:0] req_next;
    wire [2:0] max_next, min_next;
    wire idle_w, door_w, door_timer_w;

    // Sub-modules
    request_logic R1(
        .cabin_req(cabin_req),
        .hall_up(hall_up),
        .hall_down(hall_down),
        .requests_in(all_requests),
        .requests_out(req_next),
        .max_out(max_next),
        .min_out(min_next)
    );

    floor_logic F1(
        .current_floor(current_floor),
        .cabin_req(cabin_requests),
        .hall_up(up_requests),
        .hall_down(down_requests),
        .Up(Up),
        .Down(Down),
        .idle(idle_w),
        .door(door_w),
        .door_timer(door_timer_w)
    );

    // ---------------- REQUEST CLEARING LOGIC ----------------
    reg [7:0] next_cabin, next_up, next_down;
    reg emergency_mode;

    always @(*) begin
        if (emergency_mode || emergency_stop) begin
            // Clear all standard requests
            next_cabin = 0; next_up = 0; next_down = 0;
            
            // Force travel to nearest floor immediately
            if (!idle && Up && current_floor < 4) 
                next_cabin[current_floor + 1] = 1'b1;
            else if (!idle && Down && current_floor > 0) 
                next_cabin[current_floor - 1] = 1'b1;
            else
                next_cabin[current_floor] = 1'b1;
                
        end else begin
            // Normal Operations
            next_cabin = cabin_requests | cabin_req;
            next_up    = up_requests    | hall_up;
            next_down  = down_requests  | hall_down;
            
            if (idle_w) begin
                next_cabin[current_floor] = 1'b0;
                if (Up)   next_up[current_floor] = 1'b0;
                if (Down) next_down[current_floor] = 1'b0;
            end
        end
    end

    // ---------------- SEQUENTIAL BLOCK ----------------
    always @(posedge clk or posedge reset) begin
        if (reset) begin
            cabin_requests <= 0; up_requests <= 0; down_requests <= 0; all_requests <= 0;
            max_request <= 0; min_request <= 7;
            idle <= 1; door <= 0; door_timer <= 0;
            Up <= 1; Down <= 0; emergency_mode <= 0; 
        end 
        else begin
            if (emergency_stop) emergency_mode <= 1; // Lock into emergency

            cabin_requests <= next_cabin; up_requests <= next_up; down_requests <= next_down;
            all_requests   <= next_cabin | next_up | next_down;
            max_request    <= max_next; min_request <= min_next;

            if (idle_w) begin
                idle <= 1; door <= 1; door_timer <= 1; 
            end
            else if (door_timer && !emergency_mode) begin
                door <= 0; door_timer <= 0; idle <= 0; 
            end
            else if (all_requests == 0) begin
                idle <= 1; door <= 0;
            end
            else if (!door) begin
                idle <= 0;
                if (Down && (min_request < current_floor)) current_floor <= current_floor - 1;
                else if (Up && (max_request > current_floor)) current_floor <= current_floor + 1;
                else if (Up && (current_floor >= max_request)) begin Up <= 0; Down <= 1; end
                else if (Down && (current_floor <= min_request)) begin Up <= 1; Down <= 0; end
            end
        end
    end
endmodule