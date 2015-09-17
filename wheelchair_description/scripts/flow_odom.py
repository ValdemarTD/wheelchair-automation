#!/usr/bin/env python
import rospy
import tf
from geometry_msgs.msg import TransformStamped, Vector3, PoseStamped
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Imu
from px_comm.msg import OpticalFlow
import numpy as np
from tf.transformations import quaternion_from_euler, euler_from_quaternion
import PyKDL


class FlowTransformer(object):
    def __init__(self, base_frame, flow_frame, fixed_frame):
        self.base_frame = base_frame
        self.flow_frame = flow_frame
        self.fixed_frame = fixed_frame
        self.flow_ready = False
        self.last_flow_time = rospy.Time()
        self.last_odom_time = rospy.Time()

        self.last_odom_trans = (0, 0, 0)

        self.broadcaster = tf.TransformBroadcaster()
        self.listener = tf.TransformListener()
        self.transformer = tf.Transformer(interpolating=True)

        self.odom_msg = Odometry()
        self.odom_msg.header.frame_id = self.fixed_frame
        self.odom_msg.child_frame_id = self.base_frame

        self.odom_tf = TransformStamped()
        self.odom_tf.header.frame_id = self.fixed_frame
        self.odom_tf.child_frame_id = self.base_frame
        self.odom_tf.transform.rotation.w = 1

        self.imu_ang_vel = Vector3()

        # linear velocity from flow
        self.v_t = np.array((0, 0, 0))
        # angular velocity from IMU
        self.omega_t = np.array((0, 0, 0))

        # x,y,z position state vector
        self.x_t = np.array((0, 0, 0))
        self.theta_t = 0
        self.last_d_theta = 0
        self.rot = 0

        print self.base_frame, self.flow_frame
        offset = (0, 0, 0)
        try:
            self.listener.waitForTransform(
                'odom_temp',
                self.flow_frame,
                rospy.Time(),
                rospy.Duration(5)
            )
            offset = self.listener.lookupTransform('odom_temp', self.flow_frame, rospy.Time())[0]
        except tf.Exception:
            pass

        self.x_t = np.array(offset)
        self.flow_offset = self.listener.lookupTransform(self.flow_frame, self.base_frame, rospy.Time())

        rospy.Subscriber('flow', OpticalFlow, self.flow_cb)
        rospy.Subscriber('/imu/data', Imu, self.imu_cb)

        self.odom_pub = rospy.Publisher('odom', Odometry)

        rospy.Timer(rospy.Duration(0.05), self.publish_odom)

    def publish_odom(self, _):
        now = rospy.Time.now()
        self.odom_tf.header.stamp = now
        self.odom_msg.header.stamp = now

        # create a KDL frame representing the odom->flow sensor transform
        rot = quaternion_from_euler(0, 0, self.theta_t)
        f = PyKDL.Frame(
            PyKDL.Rotation.Quaternion(*rot),
            # PyKDL.Rotation.RPY(0,0,-self.rot),
            PyKDL.Vector(*self.x_t)
        )

        # find its inverse so we can add it to the TF tree
        f_inv = f.Inverse()
        trans = TransformStamped()
        trans.header.frame_id = self.flow_frame
        trans.child_frame_id = 'flow_vector'
        trans.header.stamp = now

        trans.transform.translation.x = f_inv.p.x()
        trans.transform.translation.y = f_inv.p.y()
        trans.transform.translation.z = f_inv.p.z()

        q = f_inv.M.GetQuaternion()
        trans.transform.rotation.x = q[0]
        trans.transform.rotation.y = q[1]
        trans.transform.rotation.z = q[2]
        trans.transform.rotation.w = q[3]

        # Add the flow vector to the TF tree
        self.listener.setTransform(trans)

        # Now lookup the transform in relation to base_frame
        new_trans = self.listener.lookupTransform(self.base_frame, 'flow_vector', rospy.Time(0))

        # And broadcast that transform as odom
        self.broadcaster.sendTransform(
            new_trans[0], new_trans[1], rospy.Time.now(), self.fixed_frame, self.base_frame
        )

        # TF is done

        # Now we need to create an odom message with the translation, rotation, and velocities

        # it seems silly to lookup the transform we just broadcast,
        # but this makes sure all the signs are correct
        odom_trans, odom_rot = self.listener.lookupTransform(
            self.fixed_frame,
            self.base_frame,
            rospy.Time(0)
        )

        # load up the pose
        self.odom_msg.pose.pose.position.x = odom_trans[0]
        self.odom_msg.pose.pose.position.y = odom_trans[1]
        self.odom_msg.pose.pose.position.z = odom_trans[2]

        self.odom_msg.pose.pose.orientation.x = odom_rot[0]
        self.odom_msg.pose.pose.orientation.y = odom_rot[1]
        self.odom_msg.pose.pose.orientation.z = odom_rot[2]
        self.odom_msg.pose.pose.orientation.w = odom_rot[3]

        # calculate and load up the vel
        lin_vel = np.subtract(self.last_odom_trans, odom_trans) / \
                    (now - self.last_odom_time).to_sec()
        self.odom_msg.twist.twist.linear.x = lin_vel[0]
        self.odom_msg.twist.twist.linear.y = lin_vel[1]
        self.odom_msg.twist.twist.linear.z = lin_vel[2]

        # just use the IMU for angular velocity
        self.odom_msg.twist.twist.angular = self.imu_ang_vel

        self.odom_pub.publish(self.odom_msg)


        self.last_odom_time = now
        self.last_odom_trans = odom_trans

    def imu_cb(self, msg):
        q = PyKDL.Rotation.Quaternion(
            msg.orientation.x,
            msg.orientation.y,
            msg.orientation.z,
            msg.orientation.w
        )
        self.theta_t = q.GetRPY()[2]
        self.omega_t = msg.angular_velocity.x, msg.angular_velocity.y, msg.angular_velocity.z
        self.imu_ang_vel = msg.angular_velocity

    def flow_cb(self, msg):
        dt = (msg.header.stamp - self.last_flow_time).to_sec()
        if self.flow_ready and dt > 0:
            d_theta = self.theta_t - self.last_theta
            dd_theta = d_theta - self.last_d_theta
            print dd_theta
            vel = np.array([msg.velocity_x, msg.velocity_y])
            v_body = np.linalg.norm(vel)

            # sign is taken from the sign of the egocentric forward velocity
            sign = np.sign(vel[0])
            v_body = sign * v_body

            # this is a unit vector pointing in the direction of the robot's orientation
            # in the odom frame
            u_hat_t = np.array([np.cos(self.theta_t), np.sin(self.theta_t), 0])
            v_t1 = v_body * u_hat_t

            x_t1 = self.x_t + 0.5 * dt * (v_t1 + self.v_t)

            self.v_t = v_t1
            self.x_t = x_t1

            # self.dx_t = dt * np.array([msg.velocity_x, msg.velocity_y, 0])
            # dx_t_viz = 10 * self.dx_t


            # theta_t1_viz = np.arctan2(dx_t_viz[1], self.flow_offset[0][0] + dx_t_viz[0])
            # theta_t1 = np.arctan2(self.dx_t[1], self.flow_offset[0][0] + self.dx_t[0])

            # self.broadcaster.sendTransform(
            #     dx_t_viz, quaternion_from_euler(0, 0, np.pi - theta_t1_viz), msg.header.stamp, 'next_flow_viz', self.flow_frame
            # )

            # theta_t_quat = quaternion_from_euler(0, 0, np.pi - theta_t1)
            # next_pose = PoseStamped()
            # next_pose.header.frame_id = self.flow_frame
            # next_pose.header.stamp = msg.header.stamp
            # next_pose.pose.position.x = self.dx_t[0]
            # next_pose.pose.position.y = self.dx_t[1]
            # next_pose.pose.position.z = 0

            # next_pose.pose.orientation.x = theta_t_quat[0]
            # next_pose.pose.orientation.y = theta_t_quat[1]
            # next_pose.pose.orientation.z = theta_t_quat[2]
            # next_pose.pose.orientation.w = theta_t_quat[3]

            # pose_t = self.listener.transformPose(self.base_frame, next_pose)
            # # print pose_t

            # self.broadcaster.sendTransform(
            #     (pose_t.pose.position.x, pose_t.pose.position.y, 0),
            #     (next_pose.pose.orientation.x, next_pose.pose.orientation.y, next_pose.pose.orientation.z, next_pose.pose.orientation.w),
            #     msg.header.stamp,
            #     'next_flow',
            #     self.base_frame
            # )

            # trans = l.lookupTransformFull('flow_sensor', past, 'flow_sensor', now, 'odom_temp')[0]

            # try:
            #     self.listener.waitForTransform('next_flow', self.base_frame, msg.header.stamp, rospy.Duration(5))
            #     base_trans, base_rot = self.listener.lookupTransformFull(
            #         self.base_frame,
            #         self.last_flow_time,
            #         self.base_frame,
            #         msg.header.stamp,
            #         'next_flow')
            #     # print base_movement[0]
            #     self.x_t += base_trans
            #     self.rot += euler_from_quaternion(base_rot)[-1]
            # except tf.ExtrapolationException:
            #     # this will happen the first time
            #     pass



            self.last_d_theta = dd_theta

        self.flow_ready = True
        self.last_flow_time = msg.header.stamp
        self.last_theta = self.theta_t

    def send_transform(self, transform):
        self.broadcaster.sendTransform(
            (transform.transform.translation.x,
             transform.transform.translation.y,
             transform.transform.translation.z),
            (transform.transform.rotation.x,
             transform.transform.rotation.y,
             transform.transform.rotation.z,
             transform.transform.rotation.w),
            rospy.Time.now(),
            transform.child_frame_id,
            transform.header.frame_id
        )


if __name__ == '__main__':
    rospy.init_node('flow_odom')
    base_frame = rospy.get_param('~base_frame', 'base_footprint')
    flow_frame = rospy.get_param('~flow_frame', 'flow_cam_link')
    fixed_frame = rospy.get_param('~fixed_frame', 'flow_odom')

    FlowTransformer(base_frame, flow_frame, fixed_frame)

    rospy.spin()