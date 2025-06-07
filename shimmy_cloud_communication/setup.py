from setuptools import find_packages, setup

package_name = 'shimmy_cloud_communication'
submodules = "shimmy_cloud_communication/shared_grpc"

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        # If shared_grpc contains .proto files or other non-Python files needed at runtime,
        # they might need to be included here too, or handled by package_data.
        # For Python-only .py files generated from .proto, ensuring they are part of the
        # 'shimmy_cloud_communication' Python module (e.g. in a sub-package like 'shared_grpc')
        # is usually sufficient if 'find_packages' picks them up.
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='user',
    maintainer_email='user@example.com',
    description='Package for gRPC communication with the cloud.',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'grpc_communication_node = shimmy_cloud_communication.grpc_communication_node:main',
        ],
    },
) 