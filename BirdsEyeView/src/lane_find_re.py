#!/usr/bin/env python
#-*- coding:utf-8 -*-

import cv2
import rospy
import numpy as np

from sensor_msgs.msg import Image
#from xycar_msgs.msg import xycar_motor
from cv_bridge import CvBridge

class Sliding:
    def __init__(self):
        ### subscriber, publisher
        rospy.Subscriber("/usb_cam/image_raw/", Image, self.img_callback)

        ### cvBridge
        self.bridge = CvBridge()
        self.temp_frame = np.empty(shape=[0])
        self.frame = np.empty(shape=[0])

        ### 카메라 정보
        self.img_width = 640    # 원본 이미지 가로
        self.img_height = 480    # 원본 이미지 세로

        ### xycar 캘리브레이션 보정값들
        self.cam_matrix = np.array([
            [422.037858, 0.0, 245.895397],
            [0.0, 435.589734, 163.625535],
            [0.0, 0.0, 1.0]])  # 입력 카메라 내부 행렬
        self.dist_coeffs = np.array([-0.289296, 0.061035, 0.001786, 0.015238, 0.0]) # 왜곡 계수의 입력 벡터
        self.new_cam_matrix, self.valid_pix_ROI = \
            cv2.getOptimalNewCameraMatrix(self.cam_matrix, self.dist_coeffs,(self.img_width, self.img_height), 1, (self.img_width, self.img_height))
            # cv.getOptimalNewCameraMatrix(	cameraMatrix, distCoeffs, imageSize, alpha[, newImgSize[, centerPrincipalPoint]]	) ->	retval, validPixROI
            # -> 리턴 new_camera_matrix : Output new camera intrinsic matrix.
            # the undistorted result is likely to have some black pixels corresponding to "virtual" pixels outside of the captured distorted image
            # -> calibrate_img에서 valid_pix_ROI로 영역을 다시 정하는 이유임

        ### warp 관련 값 정의 -> 원본의 절반 크기로 birds-eye view로 펼침
        self.warp_img_width = 320    # 와핑 가로
        self.warp_img_height = 240   # 와핑 세로

        warp_x_margin = 20  # 마진은 왜 주는 거야....???
        warp_y_margin = 3
        
        self.warp_src = np.array([
            [230 - warp_x_margin, 300 - warp_y_margin],
            [45 - warp_x_margin, 450 + warp_y_margin],
            [445 + warp_x_margin, 300 - warp_y_margin],
            [610 + warp_x_margin, 450 + warp_y_margin]
        ], dtype = np.float32)  # 위 왼쪽 / 아래 왼쪽 / 위 오른쪽 / 아래 오른쪽

        self.warp_dst = np.array([
            [0, 0],
            [0, self.warp_img_height],
            [self.warp_img_width, 0],
            [self.warp_img_width, self.warp_img_height]
        ], dtype = np.float32)

        ### 차선인식 관련 인수 정의
        self.l_threshold = 145  #
        self.win_num = 9    # 슬라이딩 윈도우 개수
        self.win_half_width = 12 # 슬라이딩 윈도우 가로 반절
        self.min_pixels = 5  # 선을 그리기 위한 최소 점 개수

        ### 처리 결과
        self.img = None
        self.tf_img = None
        self.perspec_mat = None
        self.perspec_mat_inv = None
        self.warp_img = None
        self.lane = None    # 이진화로 Lane 부분만 흰 색 처리한 배열
        self.left_fit = None
        self.right_fit = None

    def img_callback(self, data):
        self.frame = self.bridge.imgmsg_to_cv2(data, "bgr8")
        #self.temp_frame = self.bridge.imgmsg_to_cv2(data, "bgr8")

    def set_frame(self):
        self.frame = self.temp_frame

    ### 카메라에서 받아온 이미지를 캘리브레이션
    def calibrate_img(self):
        self.tf_img = cv2.undistort(self.frame, self.cam_matrix, self.dist_coeffs, None, self.new_cam_matrix)
            # 구한 카메라 보정 행렬을 이용해 이미지를 보정함
        x, y, w, h = self.valid_pix_ROI
        self.tf_img = self.tf_img[y:y+h, x:x+w]   # 보정 이미지 중에서 이미지의 원래 위치(?)에 맞게 잘라내기
        cv2.resize(self.tf_img, (self.img_width, self.img_height))

    ### 와핑으로 이미지 변형: birds-eye view로, 본래 영상으로 mat만들고 와핑 이미지도 반환
    def warp_image(self):
        self.perspec_mat = cv2.getPerspectiveTransform(self.warp_src, self.warp_dst)
        self.perspec_mat_inv = cv2.getPerspectiveTransform(self.warp_dst, self.warp_src)
        self.warp_img = cv2.warpPerspective(self.tf_img, self.perspec_mat, (self.warp_img_width, self.warp_img_height), flags=cv2.INTER_LINEAR)

    ### 함수 이름 다시 변경. 가우시안 블러, 채널 분리, 이진화 진행
    def binalization(self):
        blur = cv2.GaussianBlur(self.warp_img, (5, 5), 0)
        _, L, _ = cv2.split(cv2.cvtColor(blur, cv2.COLOR_BGR2HLS))
        _, self.lane = cv2.threshold(L, self.l_threshold, 255, cv2.THRESH_BINARY)  # 검은색으로 바꿀 것

    def sliding_win(self):
        ### 1. 히스토그램으로 차선의 위치를 파악
        histogram = np.sum(self.lane[self.lane.shape[0] // 2:, :], axis=0)
            # 영상의 절반 아래 부분만 히스토그램을 구함.
            # x축: 픽셀의 x좌표값, y축: 특정 x 좌표값을 갖는 모든 흰색 픽셀 개수 (1열)
        midpoint = np.int(histogram.shape[0] / 2)
        left_x_cur = np.argmax(histogram[:midpoint])    # 왼쪽 차선 픽셀 초기 설정
        right_x_cur = np.argmax(histogram[midpoint:]) + midpoint    # 오른쪽 차선 픽셀 초기 설정
        win_height = np.int(self.lane.shape[0] / self.win_num)

        nz = self.lane.nonzero()
            # 이진 영상에서 0이 아닌 부분만 저장
            # numpy.nonzero(): 2X(row*col) 형태로 반환. [x1, x2, x3, ...], [y1, y2, y3 ...] 형태
        left_lane_idx = []  # 각 윈도우마다 왼쪽 차선의 인덱스를 모음
        right_lane_idx = [] # 각 윈도우마다 오른쪽 차선의 인덱스를 모음
        lx, ly, rx, ry = [], [], [], [] # 왼쪽차선 x, y, 오른쪽차선 x, y 픽셀
        out_img = np.dstack((self.lane, self.lane, self.lane))*255 # grayscale인 것을 rgb로 바꾸고자 3차원으로 쌓고, 0~1 사이의 값을 0~255로 바꿈

        for window in range(self.win_num):
            ### 해당 슬라이딩 윈도우의 좌표를 설정
            win_lower_y = self.lane.shape[0] - (window + 1) * win_height    # 박스 위 y좌표
            win_upper_y = self.lane.shape[0] - window * win_height  # 박스 아래 y좌표
            left_win_left_x  = left_x_cur - self.win_half_width # 왼차선 박스 왼쪽 x좌표
            left_win_right_x = left_x_cur + self.win_half_width # 왼차선 박스 오른쪽 x좌표
            right_win_left_x = right_x_cur - self.win_half_width    # 오른차선 박스 왼쪽 x좌표
            right_win_right_x = right_x_cur + self.win_half_width    # 오른차선 박스 오른쪽 x좌표

            ### 왼, 오른쪽 슬라이딩 윈도우를 그림
            cv2.rectangle(out_img, (left_win_left_x, win_lower_y), (left_win_right_x, win_upper_y), (0, 255, 0), 2) # 왼차선 박스 그리기
            cv2.rectangle(out_img, (right_win_left_x, win_lower_y), (right_win_right_x, win_upper_y), (0, 255, 0), 2) # 오른차선 박스 그리기

            ### 슬라이딩 윈도우 내부의 점 중 zero가 아닌 점(차선 색을 분별해낸 픽셀)의 좌표 추출
            good_left_idx = ((nz[0] >= win_lower_y) & (nz[0] < win_upper_y) & (nz[1] >= left_win_left_x) & (nz[1] < left_win_right_x)).nonzero()[0]
            good_right_idx = ((nz[0] >= win_lower_y) & (nz[0] < win_upper_y) & (nz[1] >= right_win_left_x) & (nz[1] < right_win_right_x)).nonzero()[0]
                # 슬라이딩 윈도우 박스 하나 안의 흰 픽셀 x 좌표(열) 모두 모음.
            
            ### 해당 슬라이딩 윈도우 중 차선인 점들을 append
            left_lane_idx.append(good_left_idx)
            right_lane_idx.append(good_right_idx)

            ### 윈도우의 대표점 설정. 최소 픽셀 개수 이상 차선이면 변경
            if len(good_left_idx) > self.min_pixels:
                left_x_cur = np.int(np.mean(nz[1][good_left_idx]))
                    # 차선인 픽셀의 y좌표(가로)(ex: [0, 0, 0, 1, 1, 4, 4, 4, 4 ,4 ...])의 평균을 구함. 가장 빈번하게 나온 쪽으로 가까이 나올 것임
                    # 가장 흰색 많은 곳을 차선으로 인식하기 위함임
            if len(good_right_idx) > self.min_pixels:
                right_x_cur = np.int(np.mean(nz[1][good_right_idx]))

            ### 슬라이딩 윈도우 대표점이자 이차방정식 위의 점을 담아둠. 
            ### 슬라이딩 윈도우에서 검출되지 않으면 이전 값을 그대로 사용
            lx.append(left_x_cur)
            ly.append((win_lower_y + win_upper_y)/2)    # 세로 중점
            rx.append(right_x_cur)
            ry.append((win_lower_y + win_upper_y)/2)    # 세로 중점

        ### 라인의 x(row) 인덱스를 담은 리스트 -> 각 슬라이드마다 한 요소로 총 9개의 요소였던 것을 하나로 합침
        left_lane_idx = np.concatenate(left_lane_idx)
        right_lane_idx = np.concatenate(right_lane_idx)

        ### 9개의 점으로 왼, 오른쪽 차선을 이차식으로 피팅
        self.left_fit = np.polyfit(np.array(ly), np.array(lx), 2)
        self.right_fit = np.polyfit(np.array(ry), np.array(rx), 2)

        ### 흰색이었던 차선 픽셀을 파랑/빨강으로 변경. out_img는 B-G-R 순서의 3차원 배열임
        out_img[nz[0][left_lane_idx], nz[1][left_lane_idx]] = [255, 0, 0]   # 왼차선은 파란색
        out_img[nz[0][right_lane_idx], nz[1][right_lane_idx]] = [0, 0, 255]   # 오른차선은 빨간색
        
        cv2.imshow("line_view", out_img)

        

    def draw_line_to_src(self):
        y_max = self.warp_img.shape[0]  # 와핑 이미지의 세로 길이
        plot_y = np.linspace(0, y_max - 1, y_max) # 0부터 y_max - 1까지 y_max개의 점을 찍음
        color_warp_area = np.zeros_like(self.warp_img).astype(np.uint8)  # 원본 이미지에 그릴 탐색 영역. 와핑 이미지와 같은 모양의 0으로 채워진 배열

        ### 피팅된 왼, 오른쪽 이차식의 x 값들
        left_fit_x = self.left_fit[0]*plot_y**2 + self.left_fit[1]*plot_y + self.left_fit[2]
        right_fit_x = self.right_fit[0]*plot_y**2 + self.right_fit[1]*plot_y + self.right_fit[2]

        ### 사다리꼴 외곽선 픽셀 좌표 계산
        pts_left = np.array([np.transpose(np.vstack([left_fit_x, plot_y]))])
            # <vstack> [[x1, x2, ...], [y1, y2, ...]]
            # <transpose> [[x1, y1], [x2, y2], .... ]
            # ==> (1, 와핑 이미지 세로 길이, 2(x, y))
        pts_right = np.array([np.flipud(np.transpose(np.vstack([right_fit_x, plot_y])))])
        pts = np.hstack((pts_left, pts_right))
            # [lx1, y1], [lx2, y2], ..., [rx1, y1], [rx2, y2], ...

        line_mat = np.zeros_like(self.warp_img).astype(np.uint8)
        for p in pts:
            line_mat[p[0], p[1]] = 255

        ### 탐색 영역을 프레임에 표시
        color_warp_area = cv2.fillPoly(color_warp_area, np.int_([pts]), (0, 255, 0))    # 모든 포인트를 이은 다각형 내부를 초록색으로 표시
        src_warp = cv2.warpPerspective(color_warp_area, self.perspec_mat_inv, (self.img_width, self.img_height))    # 원근변환으로 다각형을 변환
        line_warp = cv2.warpPerspective(line_mat, self.perspec_mat_inv, (self.img_width, self.img_height))    # 원근변환으로 다각형을 변환

        self.frame = cv2.addWeighted(self.frame, 1, src_warp, 0.3, 0)    # 원본에 0.3의 투명도로 다각형을 덧그림
        cv2.imshow("camera_view1", self.frame)

        return line_warp

    # def view_frame(self, horizontal_pos, left_x, right_x, cross_pos_x, cross_pos_y):
    #     # 와핑 안 시켜줘서 이상함
    #     # cross_point가 구간 벗어나면 안 보일 듯
    #     cv2.line(self.frame, (0, horizontal_pos), (self.img_width, horizontal_pos), (0, 255, 0), 2)
    #     if cross_pos_y == -1: #V자
    #         pass
    #     elif cross_pos_y == -2: # 평행
    #         pass
    #     else:
    #         cv2.line(self.frame, (left_x, horizontal_pos), (cross_pos_x, cross_pos_y), (255, 0, 0), 2)
    #         cv2.line(self.frame, (right_x, horizontal_pos), (cross_pos_x, cross_pos_y), (255, 0, 0), 2)
    #     cv2.imshow("camera_view2", self.frame)

class Drive:
    def __init__(self):
        #self.pub = rospy.Publisher('xycar_motor', xycar_motor, queue_size=1)

        self.horizontal_pos = 350 # 교점을 찾을 가로 선. pixel
        self.vertical_half = 320 # 영상 중점. pixel

        self.left_x, self.right_x = 320, 320
        self.left_slope, self.right_slope = 1, -1

        self.cross_pos = 240 # 두 직선 교점 x좌표
        self.cross_pos_y = 0    # 두 직선 교점 y좌표
        self.pixel_error = 0 # 교점 - 화면 중점

        self.angle_kp = 0.45
        self.angle_ki = 0.0007
        self.angle_kd = 0.25
        self.angle_d_err = 0
        self.angle_p_err = 0
        self.angle_i_err = 0
        self.angle_max_i_err = 10
        self.angle_u = 0

        self.steer_angle = 0

    def find_cross_pos(self, line_warp):
        line_warp = 
        self.left_x = left_fit[0]*self.horizontal_pos**2 + left_fit[1]*self.horizontal_pos + left_fit[2]
        self.right_x = right_fit[0]*self.horizontal_pos**2 + right_fit[1]*self.horizontal_pos + right_fit[2]
        self.left_slope = 2*left_fit[0]*self.horizontal_pos + left_fit[1]    # 직선의 기울기(미분)
        self.right_slope = 2*right_fit[0]*self.horizontal_pos + right_fit[1]    # 직선의 기울기(미분)

        if self.left_slope < 0 and self.right_slope > 0:
            # 두 직선의 기울기가 V자 모양이면 이상한 상태임
            self.cross_pos = 240    # 화면 중앙으로 설정
            self.cross_pos_y = -1
        elif self.left_slope == self.right_slope: #abs(left_slope-right_slope)<10:
            # 비교값 임의 설정함. 두 직선이 거의 평행한 경우. 아마 곡선 구간일 듯
            self.cross_pos = 480    # 기울기 양수면 화면 맨 오른쪽으로 설정
            ## TODO 기울기 음수면 0으로
            self.cross_pos_y = -2
        else:
            # 아마 직선 구간. 교점 구하자
            d = (self.right_x - self.left_x) / (1-(self.left_slope/self.right_slope))
            self.cross_pos = self.left_x + d
            self.cross_pos_y = self.left_slope * d        

        self.pixel_error = self.cross_pos - self.vertical_half  # 음수면 교점이 왼쪽, 양수면 오른쪽

        if self.pixel_error < 0:
            self.pixel_error = 0
        elif self.pixel_error > 640:
            self.pixel_error = 640

    # def angle_pid(self):
    #     self.angle_d_err = self.pixel_error - self.angle_p_err
    #     self.angle_p_err = self.pixel_error
    #     self.angle_i_err += self.pixel_error
        
    #     if self.angle_i_err > self.angle_max_i_err:
    #         self.angle_i_err = 0    # 적분 초기화
        
    #     self.angle_u = (self.angle_kp * self.angle_p_err) + (self.angle_ki * self.angle_i_err) +(self.angle_kd * self.angle_d_err)
    
    def angle_pid(self, pixel_error):
        self.angle_d_err = pixel_error - self.angle_p_err
        self.angle_p_err = pixel_error
        self.angle_i_err += pixel_error
        
        if self.angle_i_err > self.angle_max_i_err:
            self.angle_i_err = 0    # 적분 초기화
        
        self.angle_u = (self.angle_kp * self.angle_p_err) + (self.angle_ki * self.angle_i_err) +(self.angle_kd * self.angle_d_err)

    def speed_pid(self):
        # self.speed = (-50) + self.kp_distance * self.dist_to_goal
        # if self.speed > self.thruster_power:
        #     self.speed = self.thruster_power
        pass

    def pub_to_motor(self, pixel_error):
        self.angle_pid(pixel_error)
        self.steer_angle = int((self.angle_u - 0) * (50 - (-50)) / (640 - 0) + (-50))
            # (x-input_min)*(output_max-output_min)/(input_max-input_min)+output_min

        ## speed_pid = self.speed_pid
        # motor_msg = xycar_motor()
        # motor_msg.speed = 30 #speed
        # motor_msg.angle = self.steer_angle
        # self.pub.publish(motor_msg) #둘다 int32

def main():
    rospy.init_node("lane_find", anonymous=True)
    prev = rospy.Time.now()

    sliding = Sliding()
    drive = Drive()

    # sliding.set_frame()

    while not rospy.is_shutdown():
        if sliding.frame.size != (640*480*3):
            print("Here")
            continue    # 640*480 이미지 한 장이 모이기 전까지 대기
        
        sliding.calibrate_img()  # 왜곡 보정하기 위해 캘리브레이션
        sliding.warp_image()    # 와핑으로 이미지 변형
        sliding.binalization()
        sliding.sliding_win()
        line_warp = sliding.draw_line_to_src()

        drive.find_cross_pos(line_warp)
    
        #sliding.view_frame(drive.horizontal_pos, int(drive.left_x), int(drive.right_x),
                            #int(drive.cross_pos), int(drive.cross_pos_y))

        time_diff = rospy.Time.now() - prev
        if time_diff.secs > 0.5:
            drive.pub_to_motor(sliding.pixel_error)
            print("="*30)
            print("left lane: {0}    right lane: {1}".format(sliding.left_x, sliding.right_x))
            print("left slope: {0}    right slope: {1}".format(sliding.left_slope, sliding.right_slope))
            print("cross_pos: ({0}, {1})    pixel_error: {2}".format(sliding.cross_pos, sliding.cross_pos_y, sliding.pixel_error))
            print("angle u: {0}    steering: {1}".format(drive.angle_u, drive.steer_angle))
            print("\n\n")
            time_diff = rospy.Time(0)
            prev = rospy.Time.now()

        # sliding.set_frame()
        if cv2.waitKey(1) == 27:
            return

    rospy.spin()


if __name__=='__main__':
    main()

"""
<수정할 점>
- 곡선의 경우 차선 간격이 다름. Day2 필기 참고
- 왼쪽/오른쪽/양쪽 차선을 잃어버린 경우
- 한쪽 차선을 찾으면 일정 거리만큼 떨어져 다른 차선이 있는가? 
    - 영상의 맨 끝을 차선의 위치로 할까, 
    - 아니면 영상 밖의 위치를 찾을까? 
    - Day1 참고
- ROI 위쪽에서는 차선 못 찾는 경우도 있었음
- PID 제어
    - 곡선에서는 느리게, 직선에서는 빠르게 주행
"""