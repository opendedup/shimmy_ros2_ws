import rclpy
from rclpy.node import Node
import asyncio
import grpc
import uuid
import threading
import traceback
from typing import Optional, AsyncGenerator

from chat_interfaces.msg import FlacVADAudio
# Assuming shared_grpc is in the same directory or correctly in Python path
# For a ROS2 package, you might need to adjust imports if shared_grpc is structured as a separate module
# If 'shared_grpc' was generated within this package structure, e.g. by a CMakeLists.txt instruction,
# the import might look like:
# from shimmy_cloud_communication.shared_grpc import shimmy_interface_pb2
# from shimmy_cloud_communication.shared_grpc import shimmy_interface_pb2_grpc
# For now, let's assume it's directly importable as it was in voice_capture_node before.
# If this causes an import error at runtime, we'll need to adjust how shared_grpc is included/generated.
try:
    from .shared_grpc import shimmy_interface_pb2
    from .shared_grpc import shimmy_interface_pb2_grpc
except ImportError as e_import:
    # This block is for handling the case where the relative import fails.
    # We log this specific failure and then re-raise to be caught by the outer Exception handler
    # or to stop the node if this is critical path.
    # The idea is to provide more specific context if the first import attempt fails.
    import sys
    print(f"Initial relative import of shared_grpc failed: {e_import}. This might indicate an issue with the Python package structure or how shared_grpc files are co-located. Ensure 'shared_grpc' is a subdirectory of the 'shimmy_cloud_communication' Python module with an __init__.py, or adjust PYTHONPATH / ROS 2 package setup accordingly.", file=sys.stderr)
    # Re-raising the error to be caught by the generic Exception below, or to halt execution if not caught.
    # This ensures that if the import is truly critical, the node doesn't proceed with missing dependencies.
    raise e_import # Re-raise the original ImportError to be caught by the outer one if needed, or stop here.
except Exception as e:
    import sys
    # Log this in a way that's visible if the node fails to start
    print(f"CRITICAL: Failed to import shared_grpc modules. Error: {e}. Ensure shimmy_interface_pb2.py and shimmy_interface_pb2_grpc.py are correctly generated and accessible in the Python path. The expected structure is often shimmy_cloud_communication/shared_grpc/ within the Python module, or that the 'shared_grpc' package is in PYTHONPATH.", file=sys.stderr)
    # Prevent node from starting if gRPC files are missing by re-raising.
    raise


# Configuration - Copied from server.py for consistency, or use ROS params
GRPC_SERVER_HOST = "localhost"  # Or get from ROS param
GRPC_SERVER_PORT = 50051        # Or get from ROS param


class GrpcCommunicationNode(Node):
    def __init__(self,namespace='/shimmy_bot'):
        super().__init__('grpc_communication_node')

        # Declare and get robot_id parameter
        self.declare_parameter('robot_id', 'default_robot_id')
        self.robot_id = self.get_parameter('robot_id').get_parameter_value().string_value
        

        self.session_id = str(uuid.uuid4())

        self.get_logger().info(f"Starting gRPC Communication Node for robot_id: '{self.robot_id}', session_id: '{self.session_id}'. Connecting to server at {GRPC_SERVER_HOST}:{GRPC_SERVER_PORT}")

        self.subscription = self.create_subscription(
            FlacVADAudio,
            f'{namespace}/vad_audio',
            self.vad_audio_callback,
            10)
        self.get_logger().info("Subscribed to 'vad_audio' topic.")

        # gRPC attributes
        self.grpc_channel = None
        self.grpc_stub = None
        self.grpc_response_stream = None # To hold the async generator from Communicate
        self.grpc_send_queue = asyncio.Queue() # Queue for RobotToCloudMessages

        # Asyncio loop and thread for gRPC
        self.async_loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.run_asyncio_loop, args=(self.async_loop,), daemon=True)
        self.loop_thread.start()
        self.get_logger().info("Asyncio event loop thread for gRPC started.")

        # Schedule gRPC client startup in the asyncio loop
        self.init_grpc_future = asyncio.run_coroutine_threadsafe(self.initialize_grpc_client(), self.async_loop)
        self.init_grpc_future.add_done_callback(self._grpc_init_done_callback)


    def _grpc_init_done_callback(self, future):
        try:
            future.result() # If an exception occurred in initialize_grpc_client, it will be raised here
            self.get_logger().info("gRPC client initialization successful and communication stream established.")
        except Exception as e:
            self.get_logger().error(f"Failed to initialize gRPC client or establish communication stream: {e}\n{traceback.format_exc()}")
            # Potentially try to reconnect or signal error state

    def run_asyncio_loop(self, loop):
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            # Cleanup tasks before loop closes
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.close()
            self.get_logger().info("Asyncio event loop for gRPC has been closed.")

    async def _message_sender(self) -> AsyncGenerator[shimmy_interface_pb2.RobotToCloudMessage, None]:
        """Async generator that yields messages from the send queue."""
        while True:
            try:
                # Wait for a message to be put on the queue
                message = await self.grpc_send_queue.get()
                if message is None: # Sentinel to indicate shutdown
                    self.get_logger().info("_message_sender: Received shutdown signal, exiting.")
                    break
                yield message
                self.grpc_send_queue.task_done()
            except asyncio.CancelledError:
                self.get_logger().info("_message_sender: Task cancelled.")
                break
            except Exception as e:
                self.get_logger().error(f"_message_sender: Error: {e}\n{traceback.format_exc()}")
                break # Exit on other errors to prevent broken state

    async def initialize_grpc_client(self):
        """Initializes the gRPC channel, stub, and starts the bi-directional stream."""
        try:
            self.get_logger().info(f"Attempting to connect to gRPC server at {GRPC_SERVER_HOST}:{GRPC_SERVER_PORT}")
            self.grpc_channel = grpc.aio.insecure_channel(f"{GRPC_SERVER_HOST}:{GRPC_SERVER_PORT}")
            
            # Optional: Wait for channel to be ready (with timeout)
            try:
                await asyncio.wait_for(self.grpc_channel.channel_ready(), timeout=10.0)
                self.get_logger().info("gRPC channel is ready.")
            except asyncio.TimeoutError:
                self.get_logger().error("gRPC channel not ready after 10 seconds. Proceeding anyway...")
                # Depending on policy, you might raise an error here or try to continue.
            
            self.grpc_stub = shimmy_interface_pb2_grpc.ShimmyCloudServiceStub(self.grpc_channel)
            self.get_logger().info("gRPC stub created.")

            # Start the bi-directional stream
            # The request_iterator for Communicate is now an async generator fed by self.grpc_send_queue
            request_iterator = self._message_sender()
            self.grpc_response_stream = self.grpc_stub.Communicate(request_iterator)
            self.get_logger().info("Called Communicate RPC. Listening for server responses.")

            # Start a task to process responses from the server
            asyncio.create_task(self.process_server_responses())

        except grpc.aio.AioRpcError as e:
            self.get_logger().error(f"gRPC connection error during initialization: {e.code()} - {e.details()}\n{traceback.format_exc()}")
            if self.grpc_channel:
                await self.grpc_channel.close()
            self.grpc_channel = None
            self.grpc_stub = None
            raise # Re-raise to be caught by _grpc_init_done_callback
        except Exception as e:
            self.get_logger().error(f"Unexpected error during gRPC initialization: {e}\n{traceback.format_exc()}")
            if self.grpc_channel:
                await self.grpc_channel.close() # Ensure channel is closed on error
            self.grpc_channel = None # Reset gRPC components
            self.grpc_stub = None
            raise # Re-raise

    async def process_server_responses(self):
        """Processes messages received from the server via the gRPC stream."""
        if not self.grpc_response_stream:
            self.get_logger().error("Cannot process server responses: gRPC response stream is not initialized.")
            return

        self.get_logger().info("process_server_responses task started.")
        try:
            async for response in self.grpc_response_stream:
                payload_type = response.WhichOneof('cloud_payload')
                self.get_logger().info(f"Received response from server: Type '{payload_type}', Session '{self.session_id}'")
                if response.HasField("text_response"):
                    self.get_logger().info(f"Server TTS for session {self.session_id}: {response.text_response.text_to_speak}")
                    # TODO: Optionally publish this to a ROS topic or handle otherwise
                elif response.HasField("robot_command"):
                    command = response.robot_command
                    command_type = command.WhichOneof('command_type')
                    self.get_logger().info(f"Server Command for session {self.session_id}: ID '{command.command_id}', Type: '{command_type}'")
                    # TODO: Handle command, send ack
                    # Example: Send an ack back
                    ack_msg = shimmy_interface_pb2.RobotToCloudMessage(
                        robot_id=self.robot_id, # Ensure robot_id is present
                        session_id=self.session_id,
                        status_update=shimmy_interface_pb2.RobotStatusUpdate(
                            command_ack=shimmy_interface_pb2.CommandAcknowledgement(
                                command_id=command.command_id,
                                message="Command Acknowledged by gRPC Comm Node"
                            )
                        )
                    )
                    await self.send_message_to_server(ack_msg)
                elif response.HasField("error_response"):
                    self.get_logger().error(f"Server Error for session {self.session_id}: {response.error_response.message}")
                # Add handling for other cloud_payload types as needed

        except grpc.aio.AioRpcError as e:
            if e.code() == grpc.StatusCode.CANCELLED:
                self.get_logger().info(f"gRPC stream cancelled (server disconnected or node shutdown): {e.details()}")
            elif e.code() == grpc.StatusCode.UNAVAILABLE:
                 self.get_logger().error(f"gRPC server unavailable: {e.details()}. Attempting to reconnect...")
                 # Simple immediate reconnect strategy (consider backoff for production)
                 await self.reconnect_grpc()
            else:
                self.get_logger().error(f"gRPC Error while processing server responses: {e.code()} - {e.details()}\n{traceback.format_exc()}")
        except Exception as e:
            self.get_logger().error(f"Unexpected error processing server responses: {e}\n{traceback.format_exc()}")
        finally:
            self.get_logger().info("process_server_responses task finished.")
            # If the stream ends, we might want to try re-establishing it.
            if self.grpc_channel and not self.grpc_channel.get_state(try_to_connect=False) == grpc.ChannelConnectivity.SHUTDOWN:
                 self.get_logger().info("Server response stream ended. Attempting to reconnect...")
                 asyncio.run_coroutine_threadsafe(self.reconnect_grpc(), self.async_loop)


    async def reconnect_grpc(self):
        """Attempts to reconnect the gRPC client."""
        self.get_logger().info("Attempting to reconnect gRPC client...")
        if self.grpc_channel:
            try:
                await self.grpc_channel.close()
            except Exception as e:
                self.get_logger().warn(f"Error closing old gRPC channel during reconnect: {e}")
        self.grpc_channel = None
        self.grpc_stub = None
        self.grpc_response_stream = None
        
        # Clear the queue as pending messages might be for an old stream context
        # Or, implement a mechanism to resend them if appropriate
        while not self.grpc_send_queue.empty():
            try:
                self.grpc_send_queue.get_nowait()
                self.grpc_send_queue.task_done()
            except asyncio.QueueEmpty:
                break
        self.get_logger().info("Cleared gRPC send queue before reconnecting.")

        try:
            await self.initialize_grpc_client()
            self.get_logger().info("gRPC client reconnected successfully.")
        except Exception as e:
            self.get_logger().error(f"Failed to reconnect gRPC client: {e}. Will retry later or on next message.\n{traceback.format_exc()}")
            # Consider a backoff strategy here

    def vad_audio_callback(self, msg: FlacVADAudio):
        """Callback for FlacVADAudio messages from 'vad_audio' topic."""
        # A session_id is now initialized for the node's lifetime.
        
        self.get_logger().info(f"Received VAD audio: robot_id='{self.robot_id}', session_id='{self.session_id}', audio_size={len(msg.audio_data)}, direction={msg.direction:.2f}")

        if not self.grpc_stub or not self.grpc_channel or not self.grpc_response_stream:
            self.get_logger().warn("gRPC client not ready, cannot send audio. Attempting to re-initialize.")
            # If called from a non-async context, ensure re-init is thread-safe
            if not (self.init_grpc_future and not self.init_grpc_future.done()): # Avoid multiple concurrent initializations
                 self.init_grpc_future = asyncio.run_coroutine_threadsafe(self.reconnect_grpc(), self.async_loop)
                 # Don't block the callback, but log that a reconnect is in progress.
                 # Messages might be dropped until connection is re-established.
            return


        robot_to_cloud_msg = shimmy_interface_pb2.RobotToCloudMessage(
            robot_id=self.robot_id, # Use robot_id from parameter
            session_id=self.session_id,  # Use newly generated session_id
            audio_chunk=shimmy_interface_pb2.AudioChunk(
                audio_data=bytes(msg.audio_data), # Convert array.array to bytes
                direction=msg.direction,
            )
        )
        
        # Add the message to the send queue for the _message_sender generator
        asyncio.run_coroutine_threadsafe(self.send_message_to_server(robot_to_cloud_msg), self.async_loop)


    async def send_message_to_server(self, message: shimmy_interface_pb2.RobotToCloudMessage):
        """Puts a message onto the gRPC send queue."""
        try:
            if not self.grpc_stub: # Or check channel connectivity
                self.get_logger().error("Cannot send message: gRPC stub not available. Attempting reconnect.")
                await self.reconnect_grpc() # Try to reconnect
                if not self.grpc_stub: # If still not available after reconnect attempt
                     self.get_logger().error("Reconnect failed. Message will be dropped.")
                     return
            
            # This is a thread-safe way to put items into an asyncio queue from a sync context
            self.async_loop.call_soon_threadsafe(self.grpc_send_queue.put_nowait, message)
            self.get_logger().info(f"Queued message for server")
        except Exception as e:
            self.get_logger().error(f"Failed to queue message for server: {e}\n{traceback.format_exc()}")


    def on_shutdown(self):
        """Called when the node is shutting down."""
        self.get_logger().info("Shutting down gRPC Communication Node...")

        # Signal the message sender to stop
        if self.async_loop and self.async_loop.is_running():
            asyncio.run_coroutine_threadsafe(self.grpc_send_queue.put(None), self.async_loop)

        # Close gRPC channel
        if self.grpc_channel and self.async_loop and self.async_loop.is_running():
            self.get_logger().info("Closing gRPC channel...")
            try:
                future = asyncio.run_coroutine_threadsafe(self.grpc_channel.close(), self.async_loop)
                future.result(timeout=2.0) # Wait for close to complete
                self.get_logger().info("gRPC channel closed.")
            except TimeoutError:
                self.get_logger().warn("Timeout closing gRPC channel.")
            except Exception as e:
                self.get_logger().error(f"Error closing gRPC channel: {e}\n{traceback.format_exc()}")
        
        # Stop asyncio loop
        if self.async_loop and self.async_loop.is_running():
            self.get_logger().info("Stopping asyncio event loop for gRPC...")
            self.async_loop.call_soon_threadsafe(self.async_loop.stop)
        
        # Wait for loop thread to finish
        if self.loop_thread and self.loop_thread.is_alive():
            self.get_logger().info("Waiting for asyncio loop thread to join...")
            self.loop_thread.join(timeout=5.0)
            if self.loop_thread.is_alive():
                self.get_logger().warn("Asyncio loop thread for gRPC did not exit cleanly.")
            else:
                self.get_logger().info("Asyncio loop thread for gRPC joined.")

        self.get_logger().info("gRPC Communication Node shutdown complete.")


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = GrpcCommunicationNode()
        if node.init_grpc_future: # Check if gRPC init was scheduled
            # Optionally, wait for initial gRPC connection before spinning, or handle failure.
            # For now, we spin and let it connect in the background.
            # Consider what happens if connection fails immediately.
            # node.init_grpc_future.result(timeout=15) # Example: wait for initial connection
            pass

        rclpy.spin(node)
    except KeyboardInterrupt:
        if node: node.get_logger().info("Keyboard interrupt, shutting down gRPC node.")
    except Exception as e:
        if node:
            node.get_logger().error(f"Unhandled exception in gRPC node main: {e}\n{traceback.format_exc()}")
        else:
            print(f"Failed to initialize gRPC node or unhandled exception: {e}\n{traceback.format_exc()}")
    finally:
        if node:
            node.on_shutdown()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

if __name__ == '__main__':
    main() 